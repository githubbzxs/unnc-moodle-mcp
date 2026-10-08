"""Moodle Web Service 客户端：REST 调用与 pluginfile 下载。

token 只放在 POST 表单里，不拼进 URL，错误信息里也只展示不带查询参数的地址，
避免 token 出现在日志、异常或返回给 AI 的文本中。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from .config import MOODLE_URL, USER_AGENT

REST_URL = f"{MOODLE_URL}/webservice/rest/server.php"
RETRIES = 2


class MoodleError(Exception):
    def __init__(self, errorcode: str, message: str) -> None:
        self.errorcode = errorcode
        self.message = message
        super().__init__(f"Moodle 返回错误（{errorcode}）：{message}")


class TokenInvalid(MoodleError):
    """token 失效或被撤销，需要重新登录。"""


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    """把嵌套参数展开成 Moodle REST 需要的 a[0][b]=c 形式。"""
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}[{k}]", v, out)
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    elif isinstance(value, bool):
        out[prefix] = int(value)
    elif value is not None:
        out[prefix] = value


def _check(data: Any) -> None:
    if isinstance(data, dict) and ("exception" in data or "errorcode" in data):
        code = str(data.get("errorcode", "unknown"))
        message = str(data.get("message") or data.get("error") or "未知错误")
        if code == "invalidtoken" or (code == "accessexception" and "token" in message.lower()):
            raise TokenInvalid(code, message)
        raise MoodleError(code, message)


def display_url(url: str) -> str:
    """去掉查询参数，供错误信息展示。"""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def ws_file_url(url: str) -> str:
    """把课程文件地址转换为 webservice/pluginfile.php，只允许本站地址。"""
    if url.startswith("/"):
        url = MOODLE_URL + url
    parsed = urlparse(url)
    if parsed.netloc.lower() != urlparse(MOODLE_URL).netloc.lower():
        raise ValueError(f"只允许下载 {MOODLE_URL} 上的文件：{display_url(url)}")
    path = parsed.path
    if "/webservice/pluginfile.php/" not in path:
        if "/pluginfile.php/" not in path:
            raise ValueError(f"不是 Moodle 文件地址（pluginfile.php）：{display_url(url)}")
        path = path.replace("/pluginfile.php/", "/webservice/pluginfile.php/", 1)
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if k != "token"])
    return urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


class MoodleClient:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=20.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> MoodleClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def call(self, function: str, **args: Any) -> Any:
        form: dict[str, Any] = {
            "wstoken": self._token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
            # 与官方 App 一致：应用文本过滤器，正文中的文件链接改写为 webservice 地址
            "moodlewssettingfilter": "true",
            "moodlewssettingfileurl": "true",
        }
        for key, value in args.items():
            _flatten(key, value, form)
        for attempt in range(RETRIES + 1):
            try:
                resp = await self._http.post(REST_URL, data=form)
                if resp.status_code >= 500 and attempt < RETRIES:
                    await asyncio.sleep(1 + attempt)
                    continue
                resp.raise_for_status()
                break
            except httpx.TransportError as exc:
                # 服务器关闭空闲连接、网络抖动等，查询是只读的，可安全重试
                if attempt < RETRIES:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise MoodleError("http", f"调用 {function} 失败：{type(exc).__name__}") from None
            except httpx.HTTPError as exc:
                raise MoodleError("http", f"调用 {function} 失败：{type(exc).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            raise MoodleError("http", f"{function} 返回的不是 JSON") from None
        _check(data)
        return data

    async def fetch_bytes(self, url: str, max_bytes: int = 5_000_000) -> bytes:
        """把小文件（如 Book 章节 HTML）读入内存。"""
        target = ws_file_url(url)
        try:
            async with self._http.stream("POST", target, data={"token": self._token}) as resp:
                if resp.status_code >= 400:
                    raise MoodleError("http", f"HTTP {resp.status_code}：{display_url(url)}")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise MoodleError("too_large", f"文件超过 {max_bytes} 字节：{display_url(url)}")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise MoodleError("http", f"读取失败（{type(exc).__name__}）：{display_url(url)}") from None
        body = b"".join(chunks)
        if body[:1] == b"{":
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                _check(json.loads(body))
        return body

    async def download(self, url: str, dest: Path) -> int:
        """下载文件到 dest（先写 .part 再原子替换），返回字节数。"""
        target = ws_file_url(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            async with self._http.stream("POST", target, data={"token": self._token}) as resp:
                if resp.status_code >= 400:
                    raise MoodleError("http", f"HTTP {resp.status_code}：{display_url(url)}")
                if "application/json" in resp.headers.get("content-type", ""):
                    body = await resp.aread()
                    try:
                        _check(json.loads(body))
                    except ValueError:
                        pass
                    tmp.write_bytes(body)
                else:
                    with tmp.open("wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            fh.write(chunk)
        except httpx.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            raise MoodleError("http", f"下载失败（{type(exc).__name__}）：{display_url(url)}") from None
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, dest)
        return dest.stat().st_size
