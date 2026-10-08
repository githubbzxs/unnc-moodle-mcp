from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import file_item, text_of


def _setup(fake):
    fake.responses["core_course_get_enrolled_courses_by_timeline_classification"] = {
        "courses": [
            {"id": 7, "fullname": "Foundation Physics (DSEEF004 UNNC)", "shortname": "DSEEF004", "viewurl": "u7"},
            {"id": 8, "fullname": "Foundation Mathematics (DSEEF002 UNNC)", "shortname": "DSEEF002", "viewurl": "u8"},
            {"id": 9, "fullname": "Research, Writing &amp; Academic Communication", "shortname": "RWAC", "viewurl": "u9"},
        ]
    }
    fake.set_contents([
        {"name": "Week 3", "summary": "<p>Read <b>chapter 3</b></p>", "modules": [
            {"id": 30, "name": "Lecture 3 slides", "modname": "resource",
             "contents": [file_item("L3.pptx", b"x" * 10, 1790000000, cmid=30)]},
            {"id": 31, "name": "Coursework 1", "modname": "assign",
             "dates": [{"label": "Due:", "timestamp": 1790500000}]},
        ]},
    ])


async def test_list_courses_and_resolve(server, fake):
    _setup(fake)
    out = text_of(await server.mcp.call_tool("list_courses", {}))
    assert "共 3 门课程" in out and "Research, Writing & Academic Communication" in out

    out = text_of(await server.mcp.call_tool("course_outline", {"course": "physics", "include_summaries": True}))
    assert "Lecture 3 slides（cmid=30）" in out and "L3.pptx" in out and "chapter 3" in out
    assert "2026-09-21" in out or "2026-09-22" in out  # 截止时间转换为 UTC+8


async def test_ambiguous_course_lists_candidates(server, fake):
    _setup(fake)
    with pytest.raises(ToolError, match=r"多门课程.*id=7"):
        await server.mcp.call_tool("course_outline", {"course": "foundation"})


async def test_find_materials(server, fake):
    _setup(fake)
    out = text_of(await server.mcp.call_tool("find_materials", {"keyword": "lecture 3"}))
    assert "cmid=30" in out


async def test_not_logged_in_message(monkeypatch):
    from unnc_moodle_mcp import auth, server

    server._state.clear()
    monkeypatch.setattr(auth, "load_session", lambda: None)
    with pytest.raises(ToolError, match="login"):
        await server.mcp.call_tool("list_courses", {})


async def test_invalid_token_clears_session(server, fake, monkeypatch):
    from unnc_moodle_mcp import auth

    cleared = []
    monkeypatch.setattr(auth, "clear_session", lambda: cleared.append(1))
    server._state["moodle"].client._token = "revoked"
    with pytest.raises(ToolError, match="失效"):
        await server.mcp.call_tool("list_courses", {})
    assert cleared


async def test_sync_and_read_local_file(server, fake, downloads):
    _setup(fake)
    out = text_of(await server.mcp.call_tool("sync_files", {"course": "7", "types": "pptx"}))
    assert "新增 1" in out
    assert (downloads / "Foundation Physics (DSEEF004 UNNC)" / "00 Week 3" / "L3.pptx").is_file()

    with pytest.raises(ToolError, match="下载目录"):
        await server.mcp.call_tool("read_local_file", {"path": "/etc/hosts"})
    with pytest.raises(ToolError, match="下载目录"):
        await server.mcp.call_tool("read_local_file", {"path": "../state/token.json"})


async def test_announcements_and_deadlines(server, fake):
    _setup(fake)
    fake.responses["mod_forum_get_forums_by_courses"] = [
        {"id": 100, "course": 7, "type": "news", "name": "Announcements"},
        {"id": 101, "course": 7, "type": "general", "name": "Q&A"},
    ]
    fake.responses["mod_forum_get_forum_discussions"] = {"discussions": [
        {"discussion": 555, "name": "Room change", "userfullname": "Dr X", "created": 1790000000,
         "message": "<p>Lecture moves to <a href='https://maps.example/pmb'>PMB</a></p>"},
    ]}
    out = text_of(await server.mcp.call_tool("announcements", {"course": "DSEEF004"}))
    assert "Room change" in out and "https://maps.example/pmb" in out and "discussion_id=555" in out

    fake.responses["core_calendar_get_action_events_by_timesort"] = {"events": [
        {"name": "Coursework 1 is due", "activityname": "Coursework 1", "timesort": 4102444800,
         "course": {"fullname": "Foundation Physics"}, "modulename": "assign", "url": "https://x"},
    ]}
    out = text_of(await server.mcp.call_tool("upcoming_deadlines", {}))
    assert "Coursework 1" in out and "剩" in out


def test_extract_pptx_and_docx(tmp_path: Path):
    from docx import Document
    from pptx import Presentation
    from pptx.util import Inches

    from unnc_moodle_mcp.extract import extract_text

    prs = Presentation()
    for i in range(3):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = f"Slide title {i + 1}"
        slide.placeholders[1].text = f"Bullet {i + 1}"
        slide.notes_slide.notes_text_frame.text = f"note {i + 1}"
    table = prs.slides[0].shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1)).table
    table.cell(0, 0).text = "F=ma"
    pptx = tmp_path / "deck.pptx"
    prs.save(pptx)
    text = extract_text(pptx, pages="2-3")
    assert "共 3 页" in text and "Slide title 2" in text and "note 3" in text and "Slide title 1" not in text
    assert "F=ma" in extract_text(pptx)

    doc = Document()
    doc.add_paragraph("Workshop handout 中文")
    docx = tmp_path / "h.docx"
    doc.save(docx)
    assert "Workshop handout 中文" in extract_text(docx)


def test_html_to_text_and_time():
    from unnc_moodle_mcp.textutil import fmt_time, html_to_text, safe_name

    html = "<p>Hello&nbsp;<b>world</b></p><ul><li>a</li><li>b</li></ul><script>x()</script>"
    assert html_to_text(html) == "Hello world\n\n- a\n- b"
    assert fmt_time(0) == "无"
    assert fmt_time(1790000000).startswith("2026-09-21 ")
    assert safe_name('a/b:c*?"<>|') == "a_b_c______"
    assert safe_name("..") == "未命名"
