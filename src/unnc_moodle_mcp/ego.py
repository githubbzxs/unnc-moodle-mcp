"""官方插件 API 不可用时，通过 EgoLite 的已登录课程页读取分页正文。"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from .config import MOODLE_URL
from .tabbed import Tab, TabbedContent, content_from_tabs

_RESULT = "UNNC_MOODLE_RESULT:"
_SCRIPT = r"""
let task;
let result;
let cleanupAllowed = true;
try {
    task = await taskSpace(opts.spaceId ?? "UNNC Moodle MCP：读取课程分页");
    const page = task.page("p1");
    const target = `${opts.base}/course/view.php?id=${opts.courseId}`;
    if (await page.url() !== target) {
        try { await page.goto(target); } catch (error) {
            if (error.name !== "PageNavigationTimeoutError") throw error;
        }
    }
    const url = new URL(await page.url());
    if (url.origin !== new URL(opts.base).origin || url.pathname.includes("/login")) {
        result = {error: "login_required"};
    } else if (url.pathname !== "/course/view.php" || url.searchParams.get("id") !== String(opts.courseId)) {
        result = {error: "wrong_page"};
    } else {
        await page.waitForSelector("#region-main", {timeout: 20000});
        result = await page.evaluate(({ids, userid}) => {
            if (Number(window.M?.cfg?.userId) !== userid) return {error: "wrong_account"};
            return {modules: ids.map(id => {
            const module = document.getElementById(`module-${id}`);
            const root = module?.querySelector('[data-region="tabbedcontent"], .mod-tabbedcontent');
            if (!root) return {id, error: "missing_module"};
            const headers = [...root.querySelectorAll('[role="tab"]')];
            const panels = [...root.querySelectorAll('[role="tabpanel"]')];
            const tabs = headers.map(header => {
                const key = header.getAttribute("aria-controls") ||
                    (header.getAttribute("data-bs-target") || header.getAttribute("href") || "").replace(/^#/, "");
                const panel = panels.find(p => p.id === key);
                return {id: key, title: header.textContent.trim(), html: panel?.innerHTML ?? null};
            });
            const complete = tabs.length > 0 && tabs.length === panels.length &&
                new Set(tabs.map(t => t.id)).size === tabs.length && tabs.every(t => t.html !== null);
            return {id, tabs: tabs.filter(t => t.html !== null), complete};
        })};
        }, {ids: opts.cmids, userid: opts.userid});
    }
} catch (error) {
    cleanupAllowed = !/control|ownership|inactive|unassigned|permission|executionStopped/i.test(`${error.name} ${error.message}`);
    result = {error: cleanupAllowed ? "browser_error" : "user_control", kind: error.name};
} finally {
    if (task && opts.spaceId === null && cleanupAllowed) {
        try { await task.finish({keep: []}); } catch { result = {error: "cleanup_failed"}; }
    }
}
console.log("UNNC_MOODLE_RESULT:" + JSON.stringify(result));
"""


class EgoUnavailable(RuntimeError):
    pass


class EgoReader:
    def __init__(self, userid: int, space_id: int | None = None) -> None:
        # 指定空间仅用于同一浏览器任务的现场验证；借用的空间由调用者关闭。
        self.space_id = space_id
        self.userid = userid
        self._lock = asyncio.Lock()
        self._sync_attempted = False

    async def _run(self, course_id: int, cmids: list[int]) -> dict:
        binary = shutil.which("ego-browser")
        if not binary:
            raise EgoUnavailable("未安装 ego-browser，无法进行网页登录态回退。")
        opts = {"base": MOODLE_URL, "courseId": int(course_id), "cmids": [int(x) for x in cmids],
                "spaceId": self.space_id, "userid": self.userid}
        proc = await asyncio.create_subprocess_exec(
            binary, "nodejs", "-e", "const opts = " + json.dumps(opts) + ";\n" + _SCRIPT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except (TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise
        # Ego 某些版本把 console.log 写到 stderr；只提取带标记的结构化结果。
        for line in reversed((stdout + b"\n" + stderr).decode("utf-8", errors="replace").splitlines()):
            if line.startswith(_RESULT):
                try:
                    result = json.loads(line[len(_RESULT):])
                    if isinstance(result, dict):
                        return result
                except ValueError:
                    break
        raise EgoUnavailable("Ego 未返回可解析的读取结果；未把浏览器原始日志写入响应。")

    async def _sync_login(self) -> bool:
        script = Path.home() / ".codex/skills/ego-login-sync/scripts/sync.sh"
        if self.space_id is not None or self._sync_attempted or not script.is_file():
            return False
        self._sync_attempted = True
        proc = await asyncio.create_subprocess_exec(
            "bash", str(script), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=120)
        except (TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise
        return proc.returncode == 0

    async def read_course(self, course_id: int, cmids: list[int]) -> dict[int, TabbedContent]:
        async with self._lock:
            try:
                data = await self._run(course_id, cmids)
                if data.get("error") == "login_required" and await self._sync_login():
                    data = await self._run(course_id, cmids)
            except TimeoutError:
                raise EgoUnavailable("Ego 读取超时，不能据此判断课件未上传。") from None
            except OSError:
                raise EgoUnavailable("无法启动 Ego 读取进程。") from None
        if data.get("error") == "login_required":
            raise EgoUnavailable("Ego 缺少学校登录态；请在 Chrome 登录并同步后重试，Moodle token 与网页登录态独立。")
        if data.get("error") == "wrong_account":
            raise EgoUnavailable("Ego 与 Moodle MCP 登录的不是同一学生账号，已拒绝混用课程内容。")
        if data.get("error") == "user_control":
            raise EgoUnavailable("Ego 控制权已转交或任务已停止，未继续操作浏览器。")
        if data.get("error"):
            raise EgoUnavailable("Ego 未完成课程页读取，不能据此判断课件未上传。")
        if not isinstance(data.get("modules"), list):
            raise EgoUnavailable("Ego 未返回课程活动列表，不能据此判断课件未上传。")
        result = {}
        for module in data.get("modules") or []:
            if module.get("error"):
                continue
            tabs = [Tab(t["id"], t["title"], t["html"]) for t in module.get("tabs") or []]
            complete = bool(module.get("complete"))
            result[int(module["id"])] = content_from_tabs(
                tabs, "Ego 已登录课程页", complete,
                "" if complete else "网页分页结构不完整，部分内容未读到。",
            )
        return result
