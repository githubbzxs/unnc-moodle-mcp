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
    ["https://evil.example/pluginfile.php/1/a.pdf", f"{BASE}/mod/resource/view.php?id=3", "file:///etc/passwd"],
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
