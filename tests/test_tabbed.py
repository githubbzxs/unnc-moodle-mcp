"""分页活动覆盖：API、网页回退、未选中分页、搜索完整性与增量下载。"""

import json
from urllib.parse import parse_qs

import pytest

from conftest import BASE, TOKEN, text_of

TABS = f"""
<nav>
  <button role="tab" aria-controls="lecture">Lecture</button>
  <button role="tab" data-bs-target="#seminar">Seminar</button>
</nav>
<div role="tabpanel" id="lecture" class="active"><p>Lecture 2: Motion</p></div>
<div role="tabpanel" id="seminar" class="tab-pane fade">
  <p>Safety seminar</p><a href="{BASE}/pluginfile.php/1/mod_tabbedcontent/tabcontent/2/Lecture%202.pptx?token=leak">Slides</a>
  <a href="https://external.example/pluginfile.php/1/a.pdf">外部文件</a>
</div>
"""
MODULES = [
    {"id": 30, "name": "Weekly content", "modname": "tabbedcontent", "uservisible": True},
    {"id": 31, "name": "Weekly content", "modname": "tabbedcontent", "uservisible": True},
]


def setup_tabbed(fake):
    fake.responses["core_course_get_enrolled_courses_by_timeline_classification"] = {"courses": [
        {"id": 7, "fullname": "Physics", "shortname": "DSEEF004-1-UNNC-AUC-2627", "viewurl": "u7"},
    ]}
    fake.set_contents([{"name": "Week 1", "modules": [dict(m) for m in MODULES]}])
    fake.responses["core_course_get_course_module"] = lambda form: {"cm": {
        "id": int(form["cmid"][0]), "course": 7, "modname": "tabbedcontent", "name": "Weekly content",
    }}
    fake.responses["tool_mobile_get_plugins_supporting_mobile"] = {"plugins": [{
        "component": "mod_tabbedcontent", "handlers": json.dumps({"view": {
            "delegate": "CoreCourseModuleDelegate", "method": "view",
        }}),
    }]}
    fake.responses["tool_mobile_get_content"] = {"templates": [{"id": "main", "html": TABS}]}
    fake.files["/webservice/pluginfile.php/1/mod_tabbedcontent/tabcontent/2/Lecture 2.pptx"] = b"example deck"


def test_mobile_parser_includes_inactive_tab_and_only_same_site_files():
    from unnc_moodle_mcp.tabbed import parse_mobile_content

    content = parse_mobile_content({"templates": [{"html": TABS}]})
    assert content.complete
    assert [t.title for t in content.tabs] == ["Lecture", "Seminar"]
    assert "Safety seminar" in content.tabs[1].html
    assert len(content.files) == 1
    assert content.files[0]["filename"] == "Lecture 2.pptx"
    assert content.files[0]["filepath"] == "/Seminar/"
    assert "token" not in content.files[0]["fileurl"]


@pytest.mark.parametrize("html", ["", "<div>Something changed</div>", '<button role="tab" aria-controls="missing">Title</button>'])
def test_mobile_parser_does_not_treat_missing_structure_as_empty_content(html):
    from unnc_moodle_mcp.tabbed import parse_mobile_content

    with pytest.raises(ValueError, match="完整"):
        parse_mobile_content({"templates": [{"html": html}]})


def test_embedded_pages_and_api_warnings_mark_incomplete():
    from unnc_moodle_mcp.tabbed import parse_mobile_content

    content = parse_mobile_content({"templates": [{"html": TABS.replace("Motion", '<iframe src="https://external.example"></iframe>')}],
                                   "warnings": [{"warningcode": "unavailable"}]})
    assert not content.complete and "嵌入页面" in content.warning


async def test_read_search_and_outline_include_tabbed_content(server, fake, monkeypatch):
    setup_tabbed(fake)

    async def no_browser(*_):
        pytest.fail("API 已完整返回，不应启动浏览器")

    monkeypatch.setattr(server._state["moodle"]._ego_reader, "read_course", no_browser)
    out = text_of(await server.mcp.call_tool("read_activity", {"cmid": 30}))
    assert "官方移动端 API" in out and "全部 2 个分页" in out
    assert "Lecture 2: Motion" in out and "Safety seminar" in out and "Lecture 2.pptx" in out
    assert "leak" not in out and "大小未知" in out
    out = text_of(await server.mcp.call_tool("find_materials", {"course": "physics", "keyword": "safety seminar"}))
    assert "cmid=30" in out and "cmid=31" in out and "Safety seminar" in out
    out = text_of(await server.mcp.call_tool("course_outline", {"course": "physics"}))
    assert "分页：Seminar" in out and "Lecture 2.pptx" in out
    form = next(parse_qs(r.content.decode()) for r in fake.requests if b"wsfunction=tool_mobile_get_content" in r.content)
    assert form["args[3][name]"] == ["appversioncode"] and form["args[3][value]"] == ["50000"]
    # 一次 API 缓存可供大纲、读取和搜索共享，不向原始内容列表注入临时字段。
    assert "_tabbed" not in (await server._state["moodle"].contents(7))[0]["modules"][0]


async def test_api_failure_batches_browser_fallback_and_caches(moodle, fake, monkeypatch):
    from unnc_moodle_mcp.tabbed import parse_mobile_content

    setup_tabbed(fake)
    fake.responses["tool_mobile_get_content"] = {"exception": "error", "errorcode": "unknown", "message": "plugin failed"}
    calls = []

    async def read(course_id, ids):
        calls.append((course_id, ids))
        content = parse_mobile_content({"templates": [{"html": TABS}]})
        content.source = "Ego 已登录课程页"
        return {i: content for i in ids}

    monkeypatch.setattr(moodle._ego_reader, "read_course", read)
    sections = await moodle.material_contents(7)
    assert calls == [(7, [30, 31])]
    assert sections[0]["modules"][0]["_tabbed"].source == "Ego 已登录课程页"
    await moodle.material_contents(7)
    assert len(calls) == 1


async def test_incomplete_results_do_not_claim_no_files(server, fake, monkeypatch):
    from unnc_moodle_mcp.ego import EgoUnavailable

    setup_tabbed(fake)
    fake.responses["tool_mobile_get_content"] = {"templates": []}

    async def unavailable(*_):
        raise EgoUnavailable("Ego 缺少学校登录态")

    monkeypatch.setattr(server._state["moodle"]._ego_reader, "read_course", unavailable)
    out = text_of(await server.mcp.call_tool("read_activity", {"cmid": 30}))
    assert "读取不完整" in out and "不代表老师未上传" in out
    out = text_of(await server.mcp.call_tool("find_materials", {"course": "physics", "keyword": "nonexistent"}))
    assert "搜索范围不完整" in out and "不代表老师未上传" in out
    out = text_of(await server.mcp.call_tool("download_activity", {"cmid": 30}))
    assert "读取不完整" in out and "没有可下载的文件" not in out
    out = text_of(await server.mcp.call_tool("sync_files", {"course": "physics", "dry_run": True}))
    assert "读取不完整" in out


async def test_hidden_tab_does_not_launch_api_or_browser(moodle, fake, monkeypatch):
    setup_tabbed(fake)

    async def no_browser(*_):
        pytest.fail("不可访问的活动不应启动浏览器")

    monkeypatch.setattr(moodle._ego_reader, "read_course", no_browser)
    content = (await moodle.tabbed_contents(7, [{"id": 30, "uservisible": False}]))[30]
    assert not content.complete and "不可访问" in content.warning
    assert not fake.requests


async def test_invalid_token_is_not_hidden_by_browser_fallback(moodle, fake):
    from unnc_moodle_mcp.client import TokenInvalid

    setup_tabbed(fake)
    fake.responses["tool_mobile_get_content"] = {"exception": "error", "errorcode": "invalidtoken", "message": "Invalid token"}
    with pytest.raises(TokenInvalid):
        await moodle.tabbed_contents(7, MODULES)


async def test_tabbed_file_download_is_incremental_and_updates(fake, moodle, downloads, monkeypatch):
    from unnc_moodle_mcp.sync import sync_course

    setup_tabbed(fake)
    course = await moodle.resolve_course("physics")
    calls = []
    metadata = {"filesize": len(b"example deck"), "timemodified": 1000}

    async def meta(url):
        calls.append(url)
        return dict(metadata)

    monkeypatch.setattr(moodle.client, "file_metadata", meta)
    first = await sync_course(moodle, course, only_cmid=30)
    assert len(first.new) == 1 and not first.failed and not first.warnings
    local = first.local_paths()[0]
    assert local.read_bytes() == b"example deck" and "/Seminar/" in local.as_posix()
    second = await sync_course(moodle, course, only_cmid=30)
    assert second.unchanged == 1 and not second.new and len(calls) == 2
    # 即使正文缓存还有效，也会重新从文件响应头发现老师替换了课件。
    fake.files["/webservice/pluginfile.php/1/mod_tabbedcontent/tabcontent/2/Lecture 2.pptx"] = b"updated deck"
    metadata.update(filesize=len(b"updated deck"), timemodified=2000)
    third = await sync_course(moodle, course, only_cmid=30)
    assert len(third.updated) == 1 and local.read_bytes() == b"updated deck"


async def test_unknown_metadata_rechecks_instead_of_treating_file_as_current(fake, moodle, downloads, monkeypatch):
    from unnc_moodle_mcp.sync import load_manifest, sync_course

    setup_tabbed(fake)

    async def meta(_):
        return {}

    monkeypatch.setattr(moodle.client, "file_metadata", meta)
    course = await moodle.resolve_course("physics")
    first = await sync_course(moodle, course, only_cmid=30)
    assert first.warnings and first.local_paths()[0].read_bytes() == b"example deck"
    assert next(iter(load_manifest(first.root).values()))["size"] == len(b"example deck")
    second = await sync_course(moodle, course, only_cmid=30)
    assert len(second.updated) == 1 and second.unchanged == 0


def test_two_same_named_files_in_one_tab_keep_distinct_stable_keys():
    from unnc_moodle_mcp.sync import plan_files
    from unnc_moodle_mcp.tabbed import Tab, content_from_tabs

    content = content_from_tabs([Tab("tab", "Lecture", (
        f'<a href="{BASE}/pluginfile.php/1/mod_tabbedcontent/tabcontent/2/slides.pdf">First</a>'
        f'<a href="{BASE}/pluginfile.php/1/mod_tabbedcontent/tabcontent/3/slides.pdf">Second</a>'
    ))], "test")
    files = plan_files([{"name": "Week 1", "modules": [{"id": 30, "name": "Weekly", "modname": "tabbedcontent",
                                                          "contents": content.files}]}])
    assert len(files) == 2 and len({f.key for f in files}) == 2
    assert {f.rel.name for f in files} == {"slides.pdf", "slides (2).pdf"}


async def test_unknown_activity_has_explicit_coverage_warning(server, fake):
    setup_tabbed(fake)
    fake.responses["core_course_get_course_module"] = {"cm": {"course": 7, "modname": "quiz", "name": "Quiz"}}
    out = text_of(await server.mcp.call_tool("read_activity", {"cmid": 30}))
    assert "读取不完整" in out and "尚未支持完整读取" in out


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_ego_reads_structured_results_on_both_streams(monkeypatch, stream):
    import unnc_moodle_mcp.ego as ego

    class Process:
        returncode = 0

        async def communicate(self):
            data = (ego._RESULT + json.dumps({"modules": []})).encode()
            return (data, b"") if stream == "stdout" else (b"", data)

    async def spawn(*args, **kwargs):
        assert "nodejs" in args
        assert "token" not in args[-1].lower()
        return Process()

    monkeypatch.setattr(ego.shutil, "which", lambda _: "/bin/ego-browser")
    monkeypatch.setattr(ego.asyncio, "create_subprocess_exec", spawn)
    assert await ego.EgoReader(42)._run(7, [30]) == {"modules": []}


async def test_ego_fails_closed_on_wrong_account(monkeypatch):
    from unnc_moodle_mcp.ego import EgoReader, EgoUnavailable

    reader = EgoReader(42)

    async def run(*_):
        return {"error": "wrong_account"}

    monkeypatch.setattr(reader, "_run", run)
    with pytest.raises(EgoUnavailable, match="同一学生账号"):
        await reader.read_course(7, [30])


async def test_ego_login_sync_retries_only_once(monkeypatch):
    from unnc_moodle_mcp.ego import EgoReader

    reader = EgoReader(42)
    calls = []

    async def run(*_):
        calls.append("read")
        return {"error": "login_required"} if len(calls) == 1 else {"modules": []}

    async def sync():
        calls.append("sync")
        return True

    monkeypatch.setattr(reader, "_run", run)
    monkeypatch.setattr(reader, "_sync_login", sync)
    assert await reader.read_course(7, [30]) == {}
    assert calls == ["read", "sync", "read"]


def test_html_output_redacts_both_href_and_url_link_text():
    from unnc_moodle_mcp.textutil import html_to_text

    url = f"{BASE}/pluginfile.php/1/a.pdf?token=leak&forcedownload=1&sesskey=private"
    text = html_to_text(f'<a href="{url}">{url}</a>')
    assert "leak" not in text and "private" not in text and "forcedownload=1" in text
