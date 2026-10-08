"""Moodle 业务封装：课程、内容、作业、公告、成绩等只读查询，带进程内短期缓存。"""

from __future__ import annotations

import asyncio
import copy
import html
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from .client import MoodleClient, MoodleError, TokenInvalid
from .ego import EgoReader, EgoUnavailable
from .tabbed import TabbedContent, parse_mobile_content

# 各类数据的缓存秒数：课程结构变化慢，通知与截止时间需要较新
TTL_COURSES = 3600
TTL_CONTENTS = 600
TTL_ACTIVITY = 300
TTL_LIVE = 60
# 结束日期在这么多天内的 past 课程仍视为本学期课程
RECENT_PAST_DAYS = 120
# UNNC 正式课程简称以课程代码开头；NONE-/FOSE-/CELE- 等是学院或机构页面
MODULE_CODE = re.compile(r"^[A-Z]{4,6}\d{3,4}[A-Z]?-")


@dataclass(frozen=True)
class Course:
    id: int
    fullname: str
    shortname: str
    category: str
    startdate: int
    enddate: int
    url: str

    @property
    def label(self) -> str:
        return f"{self.fullname}（id={self.id}）"

    @property
    def is_module(self) -> bool:
        """是否为带课程代码的正式课程（如 DSEEF011-1-UNNC-AUC-2627），而非学院/机构页面。"""
        return bool(MODULE_CODE.match(self.shortname))


class CourseNotFound(LookupError):
    pass


def section_tree(sections: list[dict]) -> list[tuple[dict, list[tuple[dict, dict | None]]]]:
    """整理 Moodle 5.x 的子章节（mod_subsection）。

    子章节在 core_course_get_contents 里既是父章节中的一个 subsection 模块，
    又作为独立章节排在末尾。这里只返回顶层章节，每个模块附带它展开的子章节（若有）。
    """
    delegated = {
        str(s.get("id")): s for s in sections if s.get("component") == "mod_subsection"
    }
    tree = []
    for section in sections:
        if section.get("component") == "mod_subsection":
            continue
        modules: list[tuple[dict, dict | None]] = []
        for module in section.get("modules", []):
            child = None
            if module.get("modname") == "subsection":
                try:
                    child = delegated.get(str(json.loads(module.get("customdata") or "{}").get("sectionid")))
                except ValueError:
                    child = None
            modules.append((module, child))
        tree.append((section, modules))
    return tree


class Moodle:
    def __init__(self, client: MoodleClient, userid: int) -> None:
        self.client = client
        self.userid = userid
        self._cache: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ego_reader = EgoReader(userid)
        self._mobile_retry_after = 0.0

    async def call(self, ttl: int, function: str, **args: Any) -> Any:
        key = function + json.dumps(args, sort_keys=True, default=str)
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            data = await self.client.call(function, **args)
            self._cache[key] = (time.monotonic() + ttl, data)
            return data

    def clear_cache(self) -> None:
        self._cache.clear()

    # ---------- 课程 ----------

    async def current_courses(self) -> list[Course]:
        """本学期课程：Moodle 的 inprogress，加上近期被归为 past 的课程。

        有的老师把课程结束日期设得过早（如 Foundation Physics 结束于开学第三周），
        已完成的课程也会被 Moodle 归入 past，这些课仍需要查看和同步。
        """
        now = time.time()
        current = await self.courses("inprogress")
        seen = {c.id for c in current}
        for c in await self.courses("past"):
            recent_end = c.enddate and c.enddate >= now - RECENT_PAST_DAYS * 86400
            recent_start = c.startdate and c.startdate >= now - 300 * 86400
            if c.id not in seen and (recent_end or recent_start):
                current.append(c)
        return sorted(current, key=lambda c: c.fullname.lower())

    async def courses(self, classification: str = "inprogress") -> list[Course]:
        data = await self.call(
            TTL_COURSES,
            "core_course_get_enrolled_courses_by_timeline_classification",
            classification=classification,
            limit=0,
        )
        result = [
            Course(
                id=int(c["id"]),
                fullname=html.unescape(c.get("fullname") or ""),
                shortname=html.unescape(c.get("shortname") or ""),
                category=html.unescape(c.get("coursecategory") or ""),
                startdate=int(c.get("startdate") or 0),
                enddate=int(c.get("enddate") or 0),
                url=c.get("viewurl") or "",
            )
            for c in data.get("courses", [])
        ]
        return sorted(result, key=lambda c: c.fullname.lower())

    async def resolve_course(self, query: str | int) -> Course:
        """按 id、简称或名称关键词定位课程；关键词有歧义时报错并列出候选。"""
        text = str(query).strip()
        # 包含用户在仪表盘上隐藏的课程
        pool = await self.courses("allincludinghidden")
        if text.isdigit():
            for c in pool:
                if c.id == int(text):
                    return c
            raise CourseNotFound(f"没有 id={text} 的已选课程")
        needle = text.lower()
        exact = [c for c in pool if needle in (c.shortname.lower(), c.fullname.lower())]
        if len(exact) == 1:
            return exact[0]
        hits = [c for c in pool if needle in c.fullname.lower() or needle in c.shortname.lower()]
        if len(hits) > 1:
            # 同名课程跨学期时优先当前学期
            current = {c.id for c in await self.current_courses()}
            active = [c for c in hits if c.id in current]
            if len(active) == 1:
                return active[0]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise CourseNotFound(f"没有匹配「{text}」的课程，可先调用 list_courses 查看")
        names = "；".join(c.label for c in hits[:10])
        raise CourseNotFound(f"「{text}」匹配到多门课程：{names}。请用课程 id 指定")

    async def contents(self, course_id: int) -> list[dict]:
        return await self.call(TTL_CONTENTS, "core_course_get_contents", courseid=course_id)

    async def _mobile_tabbed(self, course_id: int, cmid: int) -> TabbedContent:
        plugins = await self.call(TTL_COURSES, "tool_mobile_get_plugins_supporting_mobile")
        plugin = next((p for p in plugins.get("plugins", []) if p.get("component") == "mod_tabbedcontent"), {})
        handlers = json.loads(plugin.get("handlers") or "{}")
        handler = next((h for h in handlers.values() if h.get("delegate") == "CoreCourseModuleDelegate"), {})
        if not handler.get("method"):
            raise ValueError("插件未注册移动端内容接口")
        args = {"cmid": cmid, "courseid": course_id, "userid": self.userid, "appversioncode": 50000, "applang": "en"}
        data = await self.call(
            TTL_ACTIVITY, "tool_mobile_get_content", component="mod_tabbedcontent", method=handler["method"],
            args=[{"name": key, "value": str(value)} for key, value in args.items()],
        )
        return parse_mobile_content(data)

    async def tabbed_contents(self, course_id: int, modules: list[dict]) -> dict[int, TabbedContent]:
        """API 优先；插件接口故障时一次读取课程页中全部需要的分页活动。"""
        key = f"tabbed:{course_id}"
        async with self._locks.setdefault(key, asyncio.Lock()):
            result: dict[int, TabbedContent] = {}
            pending = []
            for module in modules:
                cmid = int(module["id"])
                hit = self._cache.get(f"tabbed:{course_id}:{cmid}")
                if hit and hit[0] > time.monotonic():
                    result[cmid] = hit[1]
                elif not module.get("uservisible", True):
                    result[cmid] = TabbedContent(complete=False, warning="当前用户暂不可访问该活动。")
                else:
                    pending.append(cmid)
            fallback = []
            for cmid in pending:
                if time.monotonic() < self._mobile_retry_after:
                    fallback.append(cmid)
                    continue
                try:
                    result[cmid] = await self._mobile_tabbed(course_id, cmid)
                except TokenInvalid:
                    raise
                except (MoodleError, ValueError, TypeError, KeyError):
                    self._mobile_retry_after = time.monotonic() + TTL_ACTIVITY
                    fallback.append(cmid)
            if fallback:
                try:
                    browser = await self._ego_reader.read_course(course_id, fallback)
                    for cmid in fallback:
                        result[cmid] = browser.get(cmid) or TabbedContent(
                            complete=False, warning="课程页中未找到该分页活动，可能暂不可访问或渲染结构已改变。",
                        )
                except EgoUnavailable as exc:
                    for cmid in fallback:
                        result[cmid] = TabbedContent(complete=False, warning=str(exc))
            for cmid in pending:
                item = result[cmid]
                # 完整的空分页可缓存；读取失败只短暂缓存，避免长期掩盖恢复后的内容。
                ttl = TTL_ACTIVITY if item.complete else TTL_LIVE
                self._cache[f"tabbed:{course_id}:{cmid}"] = (time.monotonic() + ttl, item)
            return result

    async def material_contents(self, course_id: int, only_cmid: int | None = None) -> list[dict]:
        """为课件搜索、下载和大纲补齐分页正文与文件，不修改原始 API 缓存。"""
        sections = copy.deepcopy(await self.contents(course_id))
        modules = [
            m for section in sections for m in section.get("modules", [])
            if m.get("modname") == "tabbedcontent" and (only_cmid is None or int(m["id"]) == only_cmid)
        ]
        if not modules:
            return sections
        contents = await self.tabbed_contents(course_id, modules)
        for module in modules:
            content = copy.deepcopy(contents[int(module["id"])])
            module["_tabbed"] = content
            known = {f.get("fileurl") for f in module.get("contents") or []}
            module["contents"] = list(module.get("contents") or []) + [f for f in content.files if f["fileurl"] not in known]
        return sections

    async def course_module(self, cmid: int) -> dict:
        data = await self.call(TTL_CONTENTS, "core_course_get_course_module", cmid=cmid)
        return data["cm"]

    async def find_module(self, course_id: int, cmid: int) -> tuple[dict, dict] | None:
        for section in await self.contents(course_id):
            for module in section.get("modules", []):
                if int(module["id"]) == cmid:
                    return section, module
        return None

    async def by_courses(self, function: str, key: str, course_id: int) -> list[dict]:
        data = await self.call(TTL_CONTENTS, function, courseids=[course_id])
        return data.get(key, [])

    # ---------- 作业与日程 ----------

    async def assignments(self, course_ids: list[int]) -> list[dict]:
        data = await self.call(TTL_ACTIVITY, "mod_assign_get_assignments", courseids=course_ids)
        result: list[dict] = []
        for course in data.get("courses", []):
            for assign in course.get("assignments", []):
                assign["_course"] = html.unescape(course.get("fullname") or "")
                result.append(assign)
        return result

    async def submission_status(self, assign_id: int) -> dict:
        return await self.call(TTL_ACTIVITY, "mod_assign_get_submission_status", assignid=assign_id)

    async def upcoming(self, days: int, limit: int) -> list[dict]:
        now = int(time.time())
        data = await self.call(
            TTL_LIVE,
            "core_calendar_get_action_events_by_timesort",
            timesortfrom=now - 3600,
            timesortto=now + days * 86400,
            limitnum=limit,
        )
        return data.get("events", [])

    # ---------- 公告与论坛 ----------

    async def forums(self, course_ids: list[int]) -> list[dict]:
        return await self.call(TTL_CONTENTS, "mod_forum_get_forums_by_courses", courseids=course_ids)

    async def discussions(self, forum_id: int, perpage: int) -> list[dict]:
        data = await self.call(
            TTL_ACTIVITY,
            "mod_forum_get_forum_discussions",
            forumid=forum_id,
            sortorder=-1,
            page=0,
            perpage=perpage,
        )
        return data.get("discussions", [])

    async def discussion_posts(self, discussion_id: int) -> list[dict]:
        data = await self.call(
            TTL_ACTIVITY,
            "mod_forum_get_discussion_posts",
            discussionid=discussion_id,
            sortby="created",
            sortdirection="ASC",
        )
        return data.get("posts", [])

    # ---------- 通知与成绩 ----------

    async def notifications(self, limit: int) -> list[dict]:
        data = await self.call(
            TTL_LIVE,
            "message_popup_get_popup_notifications",
            useridto=self.userid,
            newestfirst=1,
            limit=limit,
            offset=0,
        )
        return data.get("notifications", [])

    async def grade_overview(self) -> list[dict]:
        data = await self.call(TTL_ACTIVITY, "gradereport_overview_get_course_grades")
        return data.get("grades", [])

    async def grade_items(self, course_id: int) -> list[dict]:
        data = await self.call(
            TTL_ACTIVITY,
            "gradereport_user_get_grade_items",
            courseid=course_id,
            userid=self.userid,
        )
        users = data.get("usergrades", [])
        return users[0].get("gradeitems", []) if users else []
