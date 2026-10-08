"""测试环境：把状态目录与下载目录指向临时目录，并提供模拟的 Moodle 服务。"""

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="unnc-moodle-test-"))
os.environ["UNNC_MOODLE_STATE_DIR"] = str(_TMP / "state")
os.environ["UNNC_MOODLE_DOWNLOAD_DIR"] = str(_TMP / "downloads")
os.environ["UNNC_MOODLE_URL"] = "https://moodle.nottingham.ac.uk"

import json  # noqa: E402
from urllib.parse import parse_qs  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

BASE = "https://moodle.nottingham.ac.uk"
TOKEN = "a" * 32


def file_item(name: str, body: bytes, modified: int, path: str = "/", ctx: int = 1, cmid: int = 0) -> dict:
    return {
        "type": "file",
        "filename": name,
        "filepath": path,
        "filesize": len(body),
        "timemodified": modified,
        "fileurl": f"{BASE}/webservice/pluginfile.php/{ctx}/mod_resource/content/{cmid}/{name}?forcedownload=1",
        "_body": body,
    }


class FakeMoodle:
    """按 wsfunction 返回预设数据；记录收到的请求，便于断言 token 未泄露到 URL。"""

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.files: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self.fail_paths: set[str] = set()

    def set_contents(self, sections: list[dict]) -> None:
        for s in sections:
            for m in s.get("modules", []):
                for c in m.get("contents", []):
                    if "_body" in c:
                        self.files[httpx.URL(c["fileurl"]).path] = c.pop("_body")
        self.responses["core_course_get_contents"] = sections

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        form = parse_qs(request.content.decode()) if request.content else {}
        if request.url.path == "/webservice/rest/server.php":
            if form.get("wstoken", [""])[0] != TOKEN:
                return httpx.Response(200, json={"exception": "x", "errorcode": "invalidtoken", "message": "Invalid token"})
            fn = form["wsfunction"][0]
            data = self.responses.get(fn)
            if callable(data):
                data = data(form)
            if data is None:
                return httpx.Response(200, json={"exception": "x", "errorcode": "nofunc", "message": fn})
            return httpx.Response(200, json=data)
        if request.url.path.startswith("/webservice/pluginfile.php/"):
            if form.get("token", [""])[0] != TOKEN:
                return httpx.Response(200, json={"error": "Invalid token", "errorcode": "invalidtoken"})
            if request.url.path in self.fail_paths:
                return httpx.Response(500)
            body = self.files.get(request.url.path)
            if body is None:
                return httpx.Response(404)
            return httpx.Response(200, content=body, headers={"content-type": "application/octet-stream"})
        return httpx.Response(404)


@pytest.fixture
def fake() -> FakeMoodle:
    return FakeMoodle()


@pytest.fixture
def downloads() -> Path:
    from unnc_moodle_mcp.config import DOWNLOAD_DIR

    import shutil

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True)
    return DOWNLOAD_DIR


@pytest.fixture
def moodle(fake: FakeMoodle):
    from unnc_moodle_mcp.api import Moodle
    from unnc_moodle_mcp.client import MoodleClient

    return Moodle(MoodleClient(TOKEN, transport=httpx.MockTransport(fake.handler)), userid=42)


@pytest.fixture
def server(moodle, monkeypatch):
    """让 MCP 工具使用模拟 Moodle。"""
    from unnc_moodle_mcp import auth, server

    session = auth.Session(token=TOKEN, site=BASE, userid=42, username="scyxx1", fullname="Test Student", created_at="")
    monkeypatch.setattr(auth, "load_session", lambda: session)
    server._state["moodle"] = moodle
    yield server
    server._state.clear()


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
