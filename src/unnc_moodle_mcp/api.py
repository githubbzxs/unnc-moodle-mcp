"""Moodle 业务封装：课程、内容、作业、公告、成绩等只读查询，带进程内短期缓存。"""

from __future__ import annotations

import asyncio
import html
import json
import time
from dataclasses import dataclass
from typing import Any

from .client import MoodleClient

# 各类数据的缓存秒数：课程结构变化慢，通知与截止时间需要较新
TTL_COURSES = 3600
TTL_CONTENTS = 600
TTL_ACTIVITY = 300
TTL_LIVE = 60


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


class CourseNotFound(LookupError):
    pass


class Moodle:
    def __init__(self, client: MoodleClient, userid: int) -> None:
        self.client = client
        self.userid = userid
        self._cache: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
            current = {c.id for c in await self.courses("inprogress")}
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
