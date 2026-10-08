import httpx
import pytest

from conftest import BASE, TOKEN


def test_ws_file_url_rewrites_and_strips_token():
    from unnc_moodle_mcp.client import ws_file_url

    url = f"{BASE}/pluginfile.php/123/mod_resource/content/4/Lecture%201.pptx?forcedownload=1&token=leak"
    out = ws_file_url(url)
    assert out.startswith(f"{BASE}/webservice/pluginfile.php/123/")
    assert "token" not in out and "forcedownload=1" in out
    assert ws_file_url("/pluginfile.php/1/a/b/c.pdf") == f"{BASE}/webservice/pluginfile.php/1/a/b/c.pdf"


@pytest.mark.parametrize(
    "url",
    ["https://evil.example/pluginfile.php/1/a.pdf", f"{BASE}/mod/resource/view.php?id=3", "file:///etc/passwd",
     "http://moodle.nottingham.ac.uk/pluginfile.php/1/a.pdf"],
)
def test_ws_file_url_rejects_foreign_or_non_file(url):
    from unnc_moodle_mcp.client import ws_file_url

    with pytest.raises(ValueError):
        ws_file_url(url)


async def test_call_flattens_args_and_maps_invalid_token(fake):
    from unnc_moodle_mcp.client import MoodleClient, TokenInvalid

    seen = {}

    def assignments(form):
        seen.update(form)
        return {"courses": []}

    fake.responses["mod_assign_get_assignments"] = assignments
    async with MoodleClient(TOKEN, transport=httpx.MockTransport(fake.handler)) as c:
        await c.call("mod_assign_get_assignments", courseids=[5, 6], flag=True)
    assert seen["courseids[0]"] == ["5"] and seen["courseids[1]"] == ["6"] and seen["flag"] == ["1"]
    # token 只在请求体里
    assert all("token" not in str(r.url) for r in fake.requests)

    async with MoodleClient("wrong", transport=httpx.MockTransport(fake.handler)) as c:
        with pytest.raises(TokenInvalid):
            await c.call("core_webservice_get_site_info")


async def test_download_is_atomic_and_reports_moodle_errors(fake, tmp_path):
    from unnc_moodle_mcp.client import MoodleClient, MoodleError

    fake.files["/webservice/pluginfile.php/1/x/y/a.pdf"] = b"%PDF-1.4 hello"
    async with MoodleClient(TOKEN, transport=httpx.MockTransport(fake.handler)) as c:
        n = await c.download(f"{BASE}/pluginfile.php/1/x/y/a.pdf", tmp_path / "a.pdf")
        assert n == 14 and (tmp_path / "a.pdf").read_bytes() == b"%PDF-1.4 hello"
        with pytest.raises(MoodleError):
            await c.download(f"{BASE}/pluginfile.php/1/x/y/missing.pdf", tmp_path / "m.pdf")
    assert not (tmp_path / "m.pdf").exists() and not (tmp_path / "m.pdf.part").exists()

    async with MoodleClient("wrong", transport=httpx.MockTransport(fake.handler)) as c:
        with pytest.raises(MoodleError) as exc:
            await c.download(f"{BASE}/pluginfile.php/1/x/y/a.pdf", tmp_path / "b.pdf")
        assert "invalidtoken" in str(exc.value)
    assert not (tmp_path / "b.pdf").exists()


async def test_call_retries_dropped_connections(fake, monkeypatch):
    import unnc_moodle_mcp.client as client_mod
    from unnc_moodle_mcp.client import MoodleClient

    async def no_sleep(_):
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", no_sleep)
    fake.responses["core_webservice_get_site_info"] = {"userid": 1}
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected")
        return fake.handler(request)

    async with MoodleClient(TOKEN, transport=httpx.MockTransport(flaky)) as c:
        assert await c.call("core_webservice_get_site_info") == {"userid": 1}
    assert calls["n"] == 2


async def test_file_metadata_uses_post_without_sending_token_in_url():
    from urllib.parse import parse_qs

    from unnc_moodle_mcp.client import MoodleClient

    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert parse_qs(request.content.decode())["token"] == [TOKEN]
        assert "token" not in str(request.url)
        return httpx.Response(200, content=b"deck", headers={"last-modified": "Thu, 08 Oct 2026 06:00:00 GMT"})

    async with MoodleClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        meta = await client.file_metadata(f"{BASE}/pluginfile.php/1/a.pdf?token=ignored")
    assert meta == {"filesize": 4, "timemodified": 1791439200}
    assert len(requests) == 1


async def test_file_redirect_never_forwards_token_to_another_host(tmp_path):
    from unnc_moodle_mcp.client import MoodleClient

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://external.example/pluginfile.php/1/a.pdf"})

    async with MoodleClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="只允许下载"):
            await client.download(f"{BASE}/pluginfile.php/1/a.pdf", tmp_path / "a.pdf")
    assert len(requests) == 1 and requests[0].url.host == "moodle.nottingham.ac.uk"
    assert not (tmp_path / "a.pdf.part").exists()


async def test_file_metadata_retries_transient_connection_failures(monkeypatch):
    import unnc_moodle_mcp.client as client_mod
    from unnc_moodle_mcp.client import MoodleClient

    calls = []

    async def no_sleep(_):
        pass

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected")
        return httpx.Response(200, content=b"deck", headers={"last-modified": "Thu, 08 Oct 2026 06:00:00 GMT"})

    monkeypatch.setattr(client_mod.asyncio, "sleep", no_sleep)
    async with MoodleClient(TOKEN, transport=httpx.MockTransport(handler)) as client:
        assert (await client.file_metadata(f"{BASE}/pluginfile.php/1/a.pdf"))["filesize"] == 4
    assert len(calls) == 2
