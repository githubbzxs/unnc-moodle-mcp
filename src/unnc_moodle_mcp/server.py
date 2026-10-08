"""MCP 服务端：向 AI 暴露 UNNC Moodle 的只读查询与课件下载工具。"""

from __future__ import annotations

import asyncio
import contextlib
import html
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlparse

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import auth
from .api import Course, CourseNotFound, Moodle, section_tree
from .client import MoodleClient, MoodleError, TokenInvalid
from .config import DOWNLOAD_DIR, MOODLE_URL
from .extract import extract_text
from .sync import course_dir, sync_course
from .textutil import clip, fmt_size, fmt_time, html_to_text, safe_name

INSTRUCTIONS = f"""本服务以当前登录学生的身份只读访问宁波诺丁汉大学 Moodle（{MOODLE_URL}）。

- 任何工具提示未登录时，调用 login 一次：会弹出 Chrome 窗口，用户完成学校微软登录后自动保存 token。
- 参数 course 可以填课程 id，也可以填课程名称或简称中的关键词。
- 先用 list_courses / course_outline 找到活动的 cmid，再用 read_activity 读正文、download_activity 下载文件。
- 批量下载课件用 sync_files，文件保存在 {DOWNLOAD_DIR}/<课程>/<章节>/，只下载新增或更新的文件。
- PPT/PDF/Word 的文字用 read_local_file 读取。
- 所有时间均为 UTC+8。本服务不提交作业、不发帖、不修改任何 Moodle 数据。
"""

NOT_LOGGED_IN = "尚未登录 UNNC Moodle。请调用 login 工具，在弹出的 Chrome 窗口里完成学校登录。"

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

mcp = MCPServer("unnc-moodle", title="UNNC Moodle", instructions=INSTRUCTIONS)

_state: dict[str, Moodle] = {}


async def get_moodle() -> Moodle:
    session = auth.load_session()
    if session is None:
        raise ToolError(NOT_LOGGED_IN)
    current = _state.get("moodle")
    if current is None or current.client._token != session.token:  # noqa: SLF001
        current = Moodle(MoodleClient(session.token), session.userid)
        _state["moodle"] = current
    return current


@contextlib.contextmanager
def errors() -> Iterator[None]:
    """把内部异常转换为给 AI 看的中文错误。"""
    try:
        yield
    except TokenInvalid:
        auth.clear_session()
        _state.pop("moodle", None)
        raise ToolError("Moodle token 已失效或被撤销。" + NOT_LOGGED_IN) from None
    except (MoodleError, CourseNotFound, auth.LoginError, ValueError) as exc:
        raise ToolError(str(exc)) from None


async def _courses_for(moodle: Moodle, course: str | None) -> list[Course]:
    if course:
        return [await moodle.resolve_course(course)]
    return await moodle.current_courses()


# ---------------------------------------------------------------- 登录


@mcp.tool(
    title="登录 UNNC Moodle",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True),
)
async def login(force: bool = False) -> str:
    """弹出 Chrome 窗口让用户完成学校 SSO 登录，并在本地保存 Moodle token。

    仅在其他工具提示未登录、token 失效，或用户明确要求重新登录时调用。
    最长等待 5 分钟。

    Args:
        force: 已登录时也重新登录
    """
    with errors():
        session = auth.load_session()
        if session and not force:
            try:
                async with MoodleClient(session.token) as client:
                    await client.call("core_webservice_get_site_info")
                return f"已经是登录状态：{session.fullname}（{session.username}）。如需切换账号请传 force=true。"
            except TokenInvalid:
                auth.clear_session()
        session = await auth.browser_login()
        _state.pop("moodle", None)
        return f"登录成功：{session.fullname}（{session.username}），token 已保存在本机。"


@mcp.tool(title="登录状态", annotations=READ)
async def whoami() -> str:
    """查看当前登录的 Moodle 账号与站点信息。"""
    with errors():
        moodle = await get_moodle()
        info = await moodle.client.call("core_webservice_get_site_info")
        return (
            f"{info.get('fullname')}（{info.get('username')}，userid={info.get('userid')}）\n"
            f"站点：{info.get('sitename')}，Moodle {info.get('release')}"
        )


# ---------------------------------------------------------------- 课程与内容


@mcp.tool(title="课程列表", annotations=READ)
async def list_courses(scope: str = "inprogress") -> str:
    """列出已选课程。

    Args:
        scope: inprogress（本学期，默认）、past、future、all 或 hidden（仪表盘上隐藏的课程）
    """
    if scope not in ("inprogress", "past", "future", "all", "hidden"):
        raise ToolError("scope 只能是 inprogress、past、future、all 或 hidden")
    with errors():
        moodle = await get_moodle()
        courses = await moodle.courses(scope)
        if not courses:
            return f"没有 {scope} 课程。"
        lines = [f"共 {len(courses)} 门课程（{scope}）："]
        for c in courses:
            lines.append(f"- {c.fullname}｜简称 {c.shortname}｜id={c.id}｜{c.url}")
        return "\n".join(lines)


def _module_line(m: dict) -> str:
    name = html.unescape(m.get("name") or "")
    parts = [f"- [{m.get('modname')}] {name}（cmid={m.get('id')}）"]
    if not m.get("uservisible", True):
        parts.append("〔暂不可访问〕")
    files = [c for c in m.get("contents") or [] if c.get("type") == "file"]
    if m.get("modname") in ("resource", "folder") and files:
        if len(files) == 1:
            f = files[0]
            parts.append(f"— {f.get('filename')}，{fmt_size(f.get('filesize'))}，更新于 {fmt_time(f.get('timemodified'))}")
        else:
            total = sum(int(f.get("filesize") or 0) for f in files)
            parts.append(f"— {len(files)} 个文件，共 {fmt_size(total)}")
    if m.get("modname") == "url":
        link = next((c.get("fileurl") for c in m.get("contents") or [] if c.get("type") == "url"), None)
        if link:
            parts.append(f"— {link}")
    for d in m.get("dates") or []:
        parts.append(f"｜{d.get('label', '').rstrip(':：')} {fmt_time(d.get('timestamp'))}")
    return " ".join(parts)


@mcp.tool(title="课程大纲", annotations=READ)
async def course_outline(course: str, include_summaries: bool = False) -> str:
    """查看一门课的全部章节和活动（课件、作业、论坛、链接等），附 cmid 与文件信息。

    Args:
        course: 课程 id 或名称关键词
        include_summaries: 是否附带每个章节的说明文字和标签（label）内容
    """
    with errors():
        moodle = await get_moodle()
        c = await moodle.resolve_course(course)
        lines = [f"# {c.fullname}（id={c.id}）", c.url]

        def emit(m: dict, indent: str) -> None:
            if m.get("modname") == "label":
                if include_summaries:
                    text = html_to_text(m.get("description"))
                    if text:
                        lines.append(f"{indent}  〔标签〕{clip(text, 800)}")
                return
            lines.append(indent + _module_line(m))

        for index, (section, entries) in enumerate(section_tree(await moodle.contents(c.id))):
            summary = html_to_text(section.get("summary")) if include_summaries else ""
            if not entries and not summary:
                continue
            lines.append(f"\n## {index:02d} {html.unescape(section.get('name') or '')}")
            if summary:
                lines.append(clip(summary, 1500))
            for m, child in entries:
                if child is None:
                    emit(m, "")
                    continue
                lines.append(f"- [子章节] {html.unescape(child.get('name') or m.get('name') or '')}")
                for sub in child.get("modules", []):
                    emit(sub, "  ")
        return clip("\n".join(lines), 80_000)


@mcp.tool(title="查找课件/活动", annotations=READ)
async def find_materials(keyword: str, course: str | None = None) -> str:
    """按关键词在课程活动名和文件名中搜索（如「week 3」「lecture 5」「handbook」）。

    Args:
        keyword: 关键词，不区分大小写；多个词用空格分隔，须全部命中
        course: 只搜某门课（id 或名称关键词）；默认搜索本学期全部课程
    """
    words = [w for w in keyword.lower().split() if w]
    if not words:
        raise ToolError("keyword 不能为空")
    with errors():
        moodle = await get_moodle()
        courses = await _courses_for(moodle, course)
        all_contents = await asyncio.gather(*(moodle.contents(c.id) for c in courses))
        hits: list[str] = []
        for c, sections in zip(courses, all_contents, strict=True):
            for section in sections:
                for m in section.get("modules", []):
                    if m.get("modname") in ("subsection", "label"):
                        continue
                    names = [html.unescape(m.get("name") or "")] + [
                        f.get("filename") or "" for f in m.get("contents") or [] if f.get("type") == "file"
                    ]
                    where = html.unescape(section.get("name") or "")
                    hay = " ".join(names + [where]).lower()
                    if all(w in hay for w in words):
                        hits.append(f"{c.shortname}｜{where}\n  {_module_line(m)}")
        if not hits:
            return f"没有找到包含「{keyword}」的活动或文件。"
        return clip(f"找到 {len(hits)} 项：\n" + "\n".join(hits), 40_000)


async def _book_text(moodle: Moodle, module: dict, limit: int) -> str:
    chapters = [
        c for c in module.get("contents") or []
        if c.get("type") == "file" and c.get("filename") == "index.html"
    ]
    parts: list[str] = []
    used = 0
    for i, chapter in enumerate(chapters[:40], 1):
        body = await moodle.client.fetch_bytes(chapter["fileurl"])
        text = html_to_text(body.decode("utf-8", errors="replace"))
        parts.append(f"## 第 {i} 章\n{text}")
        used += len(text)
        if used > limit:
            break
    return "\n\n".join(parts)


@mcp.tool(title="读取活动内容", annotations=READ)
async def read_activity(cmid: int, max_chars: int = 30_000) -> str:
    """读取某个活动的正文：页面（page）、书（book）、标签、链接、作业要求、文件夹/资源文件列表等。

    文件本身用 download_activity 下载后再用 read_local_file 读取文字。

    Args:
        cmid: 活动的 cmid（来自 course_outline 或 find_materials）
        max_chars: 返回文字的最大长度
    """
    with errors():
        moodle = await get_moodle()
        cm = await moodle.course_module(cmid)
        course_id, modname = int(cm["course"]), cm["modname"]
        found = await moodle.find_module(course_id, cmid)
        section, module = found if found else ({}, {})
        lines = [
            f"# {html.unescape(cm.get('name') or '')}",
            f"类型：{modname}｜cmid={cmid}｜课程 id={course_id}｜章节：{html.unescape(section.get('name') or '')}",
            f"{MOODLE_URL}/mod/{modname}/view.php?id={cmid}",
        ]
        desc = html_to_text(module.get("description"))
        files = [c for c in module.get("contents") or [] if c.get("type") == "file"]

        if modname == "page":
            pages = await moodle.by_courses("mod_page_get_pages_by_courses", "pages", course_id)
            page = next((p for p in pages if int(p.get("coursemodule", 0)) == cmid), {})
            intro = html_to_text(page.get("intro"))
            if intro:
                lines.append(f"\n## 简介\n{intro}")
            lines.append(f"\n## 正文\n{html_to_text(page.get('content'))}")
            files = []
        elif modname == "book":
            if desc:
                lines.append(f"\n## 简介\n{desc}")
            lines.append("\n" + await _book_text(moodle, module, max_chars))
            files = [f for f in files if f.get("filename") != "index.html"]
        elif modname == "url":
            urls = await moodle.by_courses("mod_url_get_urls_by_courses", "urls", course_id)
            item = next((u for u in urls if int(u.get("coursemodule", 0)) == cmid), {})
            lines.append(f"外部链接：{item.get('externalurl', '')}")
            if desc:
                lines.append(f"\n{desc}")
        elif modname == "assign":
            assigns = await moodle.assignments([course_id])
            a = next((x for x in assigns if int(x.get("cmid", 0)) == cmid), None)
            if a:
                lines.append(_assign_dates(a))
                status = await _assign_status(moodle, a)
                lines.append(f"提交状态：{status}")
                intro = html_to_text(a.get("intro"))
                lines.append(f"\n## 作业要求\n{intro}" if intro else "\n（作业页面没有填写说明文字）")
                act = html_to_text(a.get("activity"))
                if act:
                    lines.append(f"\n## 提交说明\n{act}")
                files = list(a.get("introattachments") or []) + files
        elif modname == "forum":
            if desc:
                lines.append(f"\n{desc}")
            lines.append("\n论坛帖子请用 announcements 或 list_discussions 查看。")
        elif desc:
            lines.append(f"\n## 说明\n{desc}")

        if files:
            lines.append(f"\n## 附件（{len(files)} 个，可用 download_activity 下载）")
            for f in files[:50]:
                lines.append(f"- {f.get('filename')}（{fmt_size(f.get('filesize'))}，更新于 {fmt_time(f.get('timemodified'))}）")
        return clip("\n".join(lines), max_chars)


# ---------------------------------------------------------------- 作业与截止时间


def _assign_dates(a: dict) -> str:
    parts = [f"截止：{fmt_time(a.get('duedate'))}"]
    if a.get("cutoffdate"):
        parts.append(f"最晚：{fmt_time(a.get('cutoffdate'))}")
    if a.get("allowsubmissionsfromdate"):
        parts.append(f"开放：{fmt_time(a.get('allowsubmissionsfromdate'))}")
    return "｜".join(parts)


_SUBMISSION = {"new": "未提交", "draft": "草稿（未正式提交）", "submitted": "已提交", "reopened": "已重新开放"}
_GRADING = {"graded": "已评分", "notgraded": "未评分", "released": "成绩已发布"}


async def _assign_status(moodle: Moodle, a: dict) -> str:
    try:
        data = await moodle.submission_status(int(a["id"]))
    except MoodleError as exc:
        return f"无法获取（{exc.errorcode}）"
    last = data.get("lastattempt") or {}
    sub = last.get("submission") or last.get("teamsubmission") or {}
    opens = a.get("allowsubmissionsfromdate") or 0
    if sub.get("status") in (None, "", "new") and opens > time.time():
        status = "尚未开放"
    else:
        status = _SUBMISSION.get(sub.get("status", ""), sub.get("status") or "未提交")
    parts = [status]
    grading = _GRADING.get(last.get("gradingstatus", ""))
    if grading:
        parts.append(grading)
    grade = ((data.get("feedback") or {}).get("gradefordisplay") or "").strip()
    if grade:
        parts.append(f"成绩 {html_to_text(grade)}")
    return "，".join(parts)


@mcp.tool(title="近期截止", annotations=READ)
async def upcoming_deadlines(days: int = 14, limit: int = 50) -> str:
    """列出未来若干天内需要处理的事项（作业、测验等，与 Moodle 时间线一致），按时间排序。

    Args:
        days: 向后查看的天数
        limit: 最多条数
    """
    with errors():
        moodle = await get_moodle()
        events = await moodle.upcoming(days, limit)
        if not events:
            return f"未来 {days} 天内没有待办事项。"
        now = time.time()
        lines = [f"未来 {days} 天内共 {len(events)} 项（UTC+8）："]
        for e in events:
            ts = e.get("timesort") or e.get("timestart")
            left = (ts - now) / 86400 if ts else 0
            flag = "〔已逾期〕" if e.get("overdue") or left < 0 else f"〔剩 {left:.1f} 天〕"
            course = html.unescape((e.get("course") or {}).get("fullname") or "")
            action = (e.get("action") or {}).get("name") or ""
            lines.append(
                f"- {fmt_time(ts)} {flag} {html.unescape(e.get('activityname') or e.get('name') or '')}"
                f"｜{course}｜{e.get('modulename', '')}{('｜' + action) if action else ''}\n  {e.get('url', '')}"
            )
        return "\n".join(lines)


@mcp.tool(title="作业列表", annotations=READ)
async def list_assignments(course: str | None = None, include_past: bool = True) -> str:
    """列出作业及截止时间、提交与评分状态。

    Args:
        course: 课程 id 或名称关键词；默认本学期全部课程
        include_past: 是否包含已过截止时间的作业
    """
    with errors():
        moodle = await get_moodle()
        courses = await _courses_for(moodle, course)
        assigns = await moodle.assignments([c.id for c in courses])
        now = time.time()
        if not include_past:
            assigns = [a for a in assigns if not a.get("duedate") or a["duedate"] >= now]
        if not assigns:
            return "没有找到作业。"
        assigns.sort(key=lambda a: a.get("duedate") or 2**40)
        sem = asyncio.Semaphore(6)

        async def status(a: dict) -> str:
            async with sem:
                return await _assign_status(moodle, a)

        statuses = await asyncio.gather(*(status(a) for a in assigns))
        lines = [f"共 {len(assigns)} 个作业："]
        for a, st in zip(assigns, statuses, strict=True):
            lines.append(
                f"- {html.unescape(a.get('name') or '')}（cmid={a.get('cmid')}）｜{a['_course']}\n"
                f"  {_assign_dates(a)}｜{st}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------- 公告、论坛、通知


@mcp.tool(title="课程公告", annotations=READ)
async def announcements(course: str | None = None, limit: int = 10, days: int | None = None) -> str:
    """查看课程公告（Announcements 论坛）的最新帖子，含正文摘要。

    Args:
        course: 课程 id 或名称关键词；默认本学期全部课程
        limit: 最多帖子数
        days: 只看最近若干天
    """
    with errors():
        moodle = await get_moodle()
        courses = await _courses_for(moodle, course)
        names = {c.id: c.shortname or c.fullname for c in courses}
        forums = [f for f in await moodle.forums(list(names)) if f.get("type") == "news"]
        per_forum = await asyncio.gather(*(moodle.discussions(int(f["id"]), limit) for f in forums))
        posts: list[tuple[dict, dict]] = [(f, d) for f, ds in zip(forums, per_forum, strict=True) for d in ds]
        if days:
            since = time.time() - days * 86400
            posts = [(f, d) for f, d in posts if (d.get("created") or 0) >= since]
        posts.sort(key=lambda fd: fd[1].get("created") or 0, reverse=True)
        posts = posts[:limit]
        if not posts:
            return "没有找到公告。"
        lines = [f"最新 {len(posts)} 条公告（UTC+8）："]
        for f, d in posts:
            lines.append(
                f"\n### {html.unescape(d.get('name') or d.get('subject') or '')}\n"
                f"{names.get(int(f['course']), '')}｜{d.get('userfullname', '')}｜{fmt_time(d.get('created'))}"
                f"｜discussion_id={d.get('discussion')}"
            )
            lines.append(clip(html_to_text(d.get("message")), 1500))
            atts = d.get("attachments") or []
            if atts:
                lines.append("附件：" + "、".join(a.get("filename", "") for a in atts))
        return clip("\n".join(lines), 60_000)


@mcp.tool(title="论坛帖子列表", annotations=READ)
async def list_discussions(forum_cmid: int, limit: int = 20) -> str:
    """列出某个论坛（cmid 来自 course_outline）的讨论主题。

    Args:
        forum_cmid: 论坛活动的 cmid
        limit: 最多主题数
    """
    with errors():
        moodle = await get_moodle()
        cm = await moodle.course_module(forum_cmid)
        if cm.get("modname") != "forum":
            raise ToolError(f"cmid={forum_cmid} 不是论坛")
        discussions = await moodle.discussions(int(cm["instance"]), limit)
        if not discussions:
            return "该论坛暂无帖子。"
        lines = [f"# {html.unescape(cm.get('name') or '')}"]
        for d in discussions:
            lines.append(
                f"- {html.unescape(d.get('name') or '')}｜{d.get('userfullname', '')}｜{fmt_time(d.get('created'))}"
                f"｜回复 {d.get('numreplies', 0)}｜discussion_id={d.get('discussion')}"
            )
        return "\n".join(lines)


@mcp.tool(title="读取帖子", annotations=READ)
async def read_discussion(discussion_id: int) -> str:
    """读取一个讨论主题的全部帖子。

    Args:
        discussion_id: 来自 announcements 或 list_discussions
    """
    with errors():
        moodle = await get_moodle()
        posts = await moodle.discussion_posts(discussion_id)
        if not posts:
            return "没有帖子。"
        lines: list[str] = []
        for p in posts:
            author = (p.get("author") or {}).get("fullname", "")
            lines.append(f"\n### {html.unescape(p.get('subject') or '')}\n{author}｜{fmt_time(p.get('timecreated'))}")
            lines.append(html_to_text(p.get("message")))
            atts = p.get("attachments") or []
            if atts:
                lines.append("附件：" + "、".join(a.get("filename", "") for a in atts))
        return clip("\n".join(lines).strip(), 60_000)


@mcp.tool(title="通知", annotations=READ)
async def notifications(limit: int = 20, unread_only: bool = False) -> str:
    """查看 Moodle 通知（作业评分、截止提醒、论坛新帖等）。

    Args:
        limit: 最多条数
        unread_only: 只看未读
    """
    with errors():
        moodle = await get_moodle()
        items = await moodle.notifications(limit)
        if unread_only:
            items = [n for n in items if not n.get("read")]
        if not items:
            return "没有通知。"
        lines = [f"{len(items)} 条通知（UTC+8）："]
        for n in items:
            mark = "" if n.get("read") else "〔未读〕"
            body = n.get("smallmessage") or html_to_text(n.get("fullmessagehtml"))
            lines.append(f"- {mark}{fmt_time(n.get('timecreated'))}｜{html.unescape(n.get('subject') or '')}")
            if body and body != n.get("subject"):
                lines.append(f"  {clip(html_to_text(body), 400)}")
            if n.get("contexturl"):
                lines.append(f"  {n['contexturl']}")
        return "\n".join(lines)


@mcp.tool(title="成绩", annotations=READ)
async def grades(course: str | None = None) -> str:
    """查看成绩。不指定课程时给出各课程总评；指定课程时列出每个评分项。

    Args:
        course: 课程 id 或名称关键词
    """
    with errors():
        moodle = await get_moodle()
        if not course:
            names = {c.id: c.fullname for c in await moodle.courses("allincludinghidden")}
            rows = await moodle.grade_overview()
            if not rows:
                return "暂无成绩。"
            return "\n".join(
                ["各课程总评："]
                + [f"- {names.get(int(g['courseid']), g['courseid'])}：{g.get('grade') or '-'}" for g in rows]
            )
        c = await moodle.resolve_course(course)
        items = await moodle.grade_items(c.id)
        if not items:
            return f"{c.fullname} 暂无评分项。"
        lines = [f"# {c.fullname} 成绩"]
        for it in items:
            name = it.get("itemname") or ("课程总评" if it.get("itemtype") == "course" else it.get("itemtype"))
            lines.append(
                f"- {html.unescape(name or '')}：{it.get('gradeformatted') or '-'}"
                f"（满分 {it.get('grademax', '-')}，百分比 {it.get('percentageformatted') or '-'}，"
                f"权重 {it.get('weightformatted') or '-'}）"
            )
            feedback = html_to_text(it.get("feedback"))
            if feedback:
                lines.append(f"  反馈：{clip(feedback, 600)}")
        return "\n".join(lines)


# ---------------------------------------------------------------- 文件


def _parse_types(types: str | None) -> set[str] | None:
    if not types:
        return None
    return {t.strip().lower().lstrip(".") for t in types.replace("，", ",").split(",") if t.strip()}


@mcp.tool(title="同步课件", annotations=LOCAL_WRITE)
async def sync_files(course: str | None = None, types: str | None = None, dry_run: bool = False) -> str:
    """把课程资源（resource/folder）里的文件增量下载到本地，只下载新增或更新的文件。

    保存位置：<下载根目录>/<课程名>/<序号 章节名>/<文件>。

    Args:
        course: 课程 id 或名称关键词；默认同步本学期全部正式课程（带课程代码的，
            不含 Academic Services Office、FoSE 等学院/机构页面，这些需单独指定）
        types: 只同步这些扩展名，逗号分隔，如 "pptx,pdf"；默认全部
        dry_run: 只列出将要下载的文件，不实际下载
    """
    with errors():
        moodle = await get_moodle()
        if course:
            courses = [await moodle.resolve_course(course)]
        else:
            courses = [c for c in await moodle.current_courses() if c.is_module]
        exts = _parse_types(types)
        reports = []
        for c in courses:
            reports.append(await sync_course(moodle, c, exts, dry_run))
        head = "预览（未下载）" if dry_run else "同步完成"
        new = sum(len(r.new) for r in reports)
        upd = sum(len(r.updated) for r in reports)
        failed = sum(len(r.failed) for r in reports)
        body = "\n\n".join(r.summary() for r in reports)
        return clip(f"{head}：{len(reports)} 门课，新增 {new}、更新 {upd}、失败 {failed}。\n下载根目录：{DOWNLOAD_DIR}\n\n{body}", 60_000)


@mcp.tool(title="下载活动文件", annotations=LOCAL_WRITE)
async def download_activity(cmid: int, read: bool = False, pages: str | None = None) -> str:
    """下载某个活动（资源、文件夹、作业附件、页面附件等）里的全部文件，返回本地路径。

    Args:
        cmid: 活动 cmid
        read: 下载后直接抽取文字（PPT/PDF/Word）
        pages: 配合 read，只读部分页，如 "1-10"
    """
    with errors():
        moodle = await get_moodle()
        cm = await moodle.course_module(cmid)
        c = await moodle.resolve_course(str(cm["course"]))
        report = await sync_course(moodle, c, only_cmid=cmid)
        paths = report.local_paths()
        if cm.get("modname") == "assign" and not report.files:
            # 作业附件不在 course contents 里，单独处理
            assigns = await moodle.assignments([c.id])
            a = next((x for x in assigns if int(x.get("cmid", 0)) == cmid), None)
            for f in (a or {}).get("introattachments") or []:
                dest = course_dir(c) / "Assignments" / safe_name(a.get("name") or str(cmid)) / safe_name(f["filename"])
                if not (dest.is_file() and dest.stat().st_size == int(f.get("filesize") or -1)):
                    await moodle.client.download(f["fileurl"], dest)
                paths.append(dest)
        if not paths:
            if report.failed:
                return "下载失败：" + "；".join(f"{f.rel}：{e}" for f, e in report.failed)
            return f"cmid={cmid}（{cm.get('modname')}）没有可下载的文件。"
        lines = [f"{html.unescape(cm.get('name') or '')}：{len(paths)} 个文件"]
        lines += [f"- {p}" for p in paths]
        if read:
            for p in paths[:5]:
                lines.append("\n" + await asyncio.to_thread(extract_text, p, pages, 40_000))
        return clip("\n".join(lines), 120_000)


@mcp.tool(title="下载文件链接", annotations=LOCAL_WRITE)
async def download_url(url: str) -> str:
    """下载一个 Moodle 文件链接（pluginfile.php，例如公告或帖子正文里的附件链接）。

    Args:
        url: moodle.nottingham.ac.uk 上的 pluginfile.php 链接
    """
    with errors():
        moodle = await get_moodle()
        name = safe_name(unquote(Path(urlparse(url).path).name) or "download")
        dest = DOWNLOAD_DIR / "_links" / name
        size = await moodle.client.download(url, dest)
        return f"已保存：{dest}（{fmt_size(size)}）"


@mcp.tool(title="读取本地课件文字", annotations=READ)
async def read_local_file(path: str, pages: str | None = None, max_chars: int = 60_000) -> str:
    """抽取下载目录里 PPTX/PDF/DOCX 等文件的文字（含 PPT 讲者备注）。

    Args:
        path: 绝对路径，或相对下载根目录的路径
        pages: 页码范围，如 "1-5,8"（PDF 页 / PPT 幻灯片）
        max_chars: 返回文字的最大长度
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = DOWNLOAD_DIR / p
    p = p.resolve()
    root = DOWNLOAD_DIR.resolve()
    if not p.is_relative_to(root):
        raise ToolError(f"只能读取下载目录（{root}）中的文件")
    if not p.is_file():
        raise ToolError(f"文件不存在：{p}")
    try:
        return await asyncio.to_thread(extract_text, p, pages, max_chars)
    except Exception as exc:  # noqa: BLE001 - 损坏文件等
        raise ToolError(f"无法读取 {p.name}：{type(exc).__name__}: {exc}") from None
