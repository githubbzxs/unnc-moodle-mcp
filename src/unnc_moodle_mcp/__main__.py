"""命令行入口。

    unnc-moodle-mcp            以 stdio 方式运行 MCP 服务（供 Claude Code / Codex 调用）
    unnc-moodle-mcp login      弹出浏览器完成学校登录并保存 token
    unnc-moodle-mcp status     查看登录状态
    unnc-moodle-mcp logout     删除本机保存的 token
    unnc-moodle-mcp sync [课程] [--types pptx,pdf] [--dry-run]   不经 AI 直接同步课件
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _cmd_login(force: bool) -> int:
    from . import auth
    from .client import MoodleClient, TokenInvalid

    async def run() -> int:
        session = auth.load_session()
        if session and not force:
            try:
                async with MoodleClient(session.token) as client:
                    await client.call("core_webservice_get_site_info")
                print(f"已经是登录状态：{session.fullname}（{session.username}）。加 --force 重新登录。")
                return 0
            except TokenInvalid:
                auth.clear_session()
        print("已打开浏览器窗口，请在其中完成学校登录（最长等待 5 分钟）……")
        try:
            session = await auth.browser_login()
        except auth.LoginError as exc:
            print(f"登录失败：{exc}", file=sys.stderr)
            return 1
        print(f"登录成功：{session.fullname}（{session.username}）")
        return 0

    return asyncio.run(run())


def _cmd_status() -> int:
    from . import auth
    from .client import MoodleClient, MoodleError

    session = auth.load_session()
    if session is None:
        print("未登录。运行 `unnc-moodle-mcp login`。")
        return 1

    async def run() -> int:
        async with MoodleClient(session.token) as client:
            try:
                info = await client.call("core_webservice_get_site_info")
            except MoodleError as exc:
                print(f"token 不可用：{exc}")
                return 1
        print(f"已登录：{info.get('fullname')}（{info.get('username')}），Moodle {info.get('release')}")
        print(f"token 获取时间：{session.created_at}")
        return 0

    return asyncio.run(run())


def _cmd_logout() -> int:
    from . import auth

    print("已删除本机 token。" if auth.clear_session() else "本机没有保存 token。")
    print("如需同时让服务器端 token 失效，可在 Moodle「Preferences → Security keys」中重置。")
    return 0


def _cmd_sync(course: str | None, types: str | None, dry_run: bool) -> int:
    from .server import errors, sync_files

    async def run() -> int:
        try:
            with errors():
                print(await sync_files(course=course, types=types, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001 - 命令行直接报错退出
            print(f"同步失败：{exc}", file=sys.stderr)
            return 1
        return 0

    return asyncio.run(run())


def main() -> None:
    parser = argparse.ArgumentParser(prog="unnc-moodle-mcp", description="UNNC Moodle MCP 服务与命令行工具")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="以 stdio 方式运行 MCP 服务（默认）")
    p_login = sub.add_parser("login", help="浏览器登录并保存 token")
    p_login.add_argument("--force", action="store_true", help="已登录时也重新登录")
    sub.add_parser("status", help="查看登录状态")
    sub.add_parser("logout", help="删除本机 token")
    p_sync = sub.add_parser("sync", help="增量同步课件")
    p_sync.add_argument("course", nargs="?", help="课程 id 或名称关键词，默认本学期全部")
    p_sync.add_argument("--types", help="只同步这些扩展名，如 pptx,pdf")
    p_sync.add_argument("--dry-run", action="store_true", help="只预览不下载")
    args = parser.parse_args()

    if args.cmd in (None, "serve"):
        from .server import mcp

        mcp.run(transport="stdio")
        return
    if args.cmd == "login":
        sys.exit(_cmd_login(args.force))
    if args.cmd == "status":
        sys.exit(_cmd_status())
    if args.cmd == "logout":
        sys.exit(_cmd_logout())
    if args.cmd == "sync":
        sys.exit(_cmd_sync(args.course, args.types, args.dry_run))


if __name__ == "__main__":
    main()
