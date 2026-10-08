"""登录：用独立 Chrome 配置目录完成学校 SSO，换取 Moodle 移动端 Web Service token。

流程与官方 Moodle App 相同：打开 admin/tool/mobile/launch.php，用户在浏览器里走完
「Student and staff sign in」→ 微软登录，Moodle 随后重定向到
moodlemobile://token=<base64(md5(站点地址+passport):::token[:::privatetoken])>。
这里只截获该重定向并校验 passport，不读取浏览器 Cookie，也不保存 privatetoken。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .client import MoodleClient
from .config import (
    BROWSER_PROFILE,
    LOGIN_TIMEOUT_S,
    MOBILE_SERVICE,
    MOODLE_URL,
    STATE_DIR,
    TOKEN_FILE,
    TZ,
)

_APP_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://token=(?P<payload>[A-Za-z0-9+/=_\-]+)")
# 学校登录页上的 SSO 按钮，链接形如 /auth/saml2/login.php?wants=...&idp=...
_SSO_LINK = "a[href*='/auth/saml2/login.php']"


class LoginError(Exception):
    pass


@dataclass(frozen=True)
class Session:
    token: str
    site: str
    userid: int
    username: str
    fullname: str
    created_at: str


def load_session() -> Session | None:
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        session = Session(**data)
    except (OSError, ValueError, TypeError):
        return None
    return session if session.site == MOODLE_URL else None


def save_session(session: Session) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(session), fh, ensure_ascii=False, indent=2)
    os.replace(tmp, TOKEN_FILE)


def clear_session() -> bool:
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        return True
    return False


def decode_app_token(url: str, passport: str) -> str:
    """解析 moodlemobile://token=...，校验站点与 passport 后返回 token。"""
    match = _APP_SCHEME.match(url)
    if not match:
        raise LoginError("回调地址格式不正确")
    payload = match.group("payload")
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.b64decode(payload).decode()
    except ValueError:
        raise LoginError("无法解析 Moodle 返回的 token") from None
    parts = raw.split(":::")
    expected = hashlib.md5(f"{MOODLE_URL}{passport}".encode()).hexdigest()
    if len(parts) < 2 or parts[0] != expected:
        raise LoginError("token 的站点或 passport 校验失败，已拒绝")
    if not re.fullmatch(r"[A-Za-z0-9]{16,}", parts[1]):
        raise LoginError("token 格式异常，已拒绝")
    return parts[1]


async def _launch_context(playwright: Any) -> Any:
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    last: Exception | None = None
    # 优先用本机 Google Chrome；没有时退回 Playwright 自带 Chromium
    for kwargs in ({"channel": "chrome"}, {}):
        try:
            return await playwright.chromium.launch_persistent_context(
                str(BROWSER_PROFILE),
                headless=False,
                no_viewport=True,
                args=["--no-first-run", "--no-default-browser-check"],
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - 逐个尝试可用浏览器
            last = exc
    raise LoginError(
        "找不到可用的浏览器。请安装 Google Chrome，或运行 "
        "`uv run playwright install chromium`。"
    ) from last


async def browser_login(timeout_s: int = LOGIN_TIMEOUT_S) -> Session:
    """打开浏览器窗口等待用户完成 SSO，截获 token 并保存。"""
    from playwright.async_api import async_playwright

    passport = secrets.token_hex(8)
    launch_url = (
        f"{MOODLE_URL}/admin/tool/mobile/launch.php"
        f"?service={MOBILE_SERVICE}&passport={passport}&urlscheme=moodlemobile"
    )
    loop = asyncio.get_running_loop()
    captured: asyncio.Future[str] = loop.create_future()

    def offer(url: str | None) -> None:
        if url and _APP_SCHEME.match(url) and not captured.done():
            captured.set_result(url)

    def on_request(request: Any) -> None:
        offer(request.url)

    def on_response(response: Any) -> None:
        offer(response.headers.get("location"))

    async with async_playwright() as p:
        context = await _launch_context(p)
        try:
            context.on("request", on_request)
            context.on("response", on_response)
            page = context.pages[0] if context.pages else await context.new_page()
            with contextlib.suppress(Exception):
                await page.goto(launch_url, wait_until="domcontentloaded", timeout=60_000)
            # 未登录时会停在 Moodle 登录页，自动点学校 SSO 按钮，用户只需完成微软登录
            if not captured.done() and "/login/index.php" in page.url:
                with contextlib.suppress(Exception):
                    await page.click(_SSO_LINK, timeout=5_000)
            try:
                url = await asyncio.wait_for(captured, timeout=timeout_s)
            except TimeoutError:
                raise LoginError(
                    f"{timeout_s // 60} 分钟内未完成登录，已取消。请重新运行登录。"
                ) from None
        finally:
            with contextlib.suppress(Exception):
                await context.close()

    token = decode_app_token(url, passport)
    async with MoodleClient(token) as client:
        info = await client.call("core_webservice_get_site_info")
    session = Session(
        token=token,
        site=MOODLE_URL,
        userid=int(info["userid"]),
        username=str(info.get("username", "")),
        fullname=str(info.get("fullname", "")),
        created_at=datetime.now(TZ).isoformat(timespec="seconds"),
    )
    save_session(session)
    return session
