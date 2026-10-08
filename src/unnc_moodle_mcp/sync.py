"""课件增量同步：把课程里的资源、文件夹附件下载到本地 <课程>/<章节>/ 目录。

每门课的目录下有一个 .moodle-sync.json 记录已下载文件的修改时间与大小，
再次同步时只下载新增或在 Moodle 上被更新过的文件；用户自己放进目录的同名文件不会被覆盖。
"""

from __future__ import annotations

import asyncio
import html
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .api import Course, Moodle, section_tree
from .client import MoodleError, TokenInvalid
from .config import DOWNLOAD_DIR
from .textutil import fmt_size, safe_name

MANIFEST = ".moodle-sync.json"
# 默认同步这些模块里的文件；page 模块的内嵌图片和 HTML 通常没有单独保存价值
SYNC_MODULES = {"resource", "folder", "tabbedcontent"}
CONCURRENCY = 4


@dataclass(frozen=True)
class RemoteFile:
    key: str
    url: str
    rel: Path
    size: int
    modified: int
    section: str
    module: str
    cmid: int
    metadata_known: bool = True


@dataclass
class SyncReport:
    course: Course
    root: Path
    new: list[RemoteFile] = field(default_factory=list)
    updated: list[RemoteFile] = field(default_factory=list)
    unchanged: int = 0
    failed: list[tuple[RemoteFile, str]] = field(default_factory=list)
    dry_run: bool = False
    files: dict[str, Path] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def local_paths(self) -> list[Path]:
        """本次涉及且已在本地的文件绝对路径（按清单记录的实际位置）。"""
        manifest = load_manifest(self.root)
        paths = []
        for key in self.files:
            entry = manifest.get(key)
            if entry and (self.root / entry["path"]).is_file():
                paths.append(self.root / entry["path"])
        return paths

    def summary(self, show: int = 30) -> str:
        verb = "将下载" if self.dry_run else "已下载"
        lines = [
            f"## {self.course.fullname}",
            f"目录：{self.root}",
            f"新增 {len(self.new)} 个、更新 {len(self.updated)} 个、"
            f"未变化 {self.unchanged} 个、失败 {len(self.failed)} 个",
        ]
        changed = [("新增", f) for f in self.new] + [("更新", f) for f in self.updated]
        if changed:
            lines.append(f"{verb}：")
            for tag, f in changed[:show]:
                size = fmt_size(f.size) if f.metadata_known else "大小或更新时间未知"
                lines.append(f"- [{tag}] {f.rel}（{size}）")
            if len(changed) > show:
                lines.append(f"- …另有 {len(changed) - show} 个")
        for f, err in self.failed[:10]:
            lines.append(f"- [失败] {f.rel}：{err}")
        for warning in self.warnings:
            lines.append(f"- [读取不完整] {warning}")
        return "\n".join(lines)


def course_dir(course: Course, base: Path = DOWNLOAD_DIR) -> Path:
    return base / safe_name(course.fullname or course.shortname or str(course.id))


def _match_ext(name: str, exts: set[str] | None) -> bool:
    return not exts or Path(name).suffix.lower().lstrip(".") in exts


def plan_files(
    sections: list[dict],
    exts: set[str] | None = None,
    modules: set[str] = SYNC_MODULES,
) -> list[RemoteFile]:
    """从 core_course_get_contents 的结果列出要同步的文件及其本地相对路径。

    子章节（Moodle 5.x 的 mod_subsection）里的文件放在父章节下的子目录中。
    """
    files: list[RemoteFile] = []
    taken: set[str] = set()

    def add(module: dict, section_dir: Path, section_name: str) -> None:
        modname = module.get("modname", "")
        if modname not in modules or not module.get("uservisible", True):
            return
        items = [c for c in module.get("contents") or [] if c.get("type") == "file"]
        base = section_dir
        if modname in {"folder", "tabbedcontent"} or len(items) > 1:
            base = section_dir / safe_name(html.unescape(module.get("name") or "folder"))
        for item in items:
            name = item.get("filename") or ""
            url = item.get("fileurl") or ""
            if not name or not url or not _match_ext(name, exts):
                continue
            sub = [safe_name(p) for p in (item.get("filepath") or "/").strip("/").split("/") if p]
            rel = base.joinpath(*sub, safe_name(name))
            # 同目录下重名时追加序号
            stem, suffix, n = rel.stem, rel.suffix, 2
            while rel.as_posix().lower() in taken:
                rel = rel.with_name(f"{stem} ({n}){suffix}")
                n += 1
            taken.add(rel.as_posix().lower())
            files.append(
                RemoteFile(
                    # 资源文件 URL 里含版本号，替换文件后会变；用 cmid+路径+文件名作稳定键
                    key=(f"{module.get('id')}:tabfile:{item['_file_key']}" if "_file_key" in item
                         else f"{module.get('id')}:{item.get('filepath') or '/'}{name}"),
                    url=url,
                    rel=rel,
                    size=int(item.get("filesize") or 0),
                    modified=int(item.get("timemodified") or 0),
                    section=section_name,
                    module=html.unescape(module.get("name") or ""),
                    cmid=int(module.get("id") or 0),
                    metadata_known=not item.get("_metadata_unknown", False),
                )
            )

    for index, (section, entries) in enumerate(section_tree(sections)):
        section_name = html.unescape(section.get("name") or "").strip() or f"Section {index}"
        section_dir = Path(safe_name(f"{index:02d} {section_name}"))
        for module, child in entries:
            if child is None:
                add(module, section_dir, section_name)
                continue
            child_name = html.unescape(child.get("name") or module.get("name") or "").strip()
            for sub_module in child.get("modules", []):
                add(sub_module, section_dir / safe_name(child_name or "subsection"), section_name)
    return files


def load_manifest(root: Path) -> dict[str, dict]:
    try:
        data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        return data.get("files", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_manifest(root: Path, course: Course, files: dict[str, dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (MANIFEST + ".tmp")
    payload = {"course_id": course.id, "course": course.fullname, "files": files}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, root / MANIFEST)


def _is_current(entry: dict | None, f: RemoteFile, root: Path) -> bool:
    if not entry:
        return False
    local = root / entry.get("path", "")
    return (
        f.metadata_known
        and entry.get("modified") == f.modified
        and entry.get("size") == f.size
        and local.is_file()
        and local.stat().st_size == f.size
    )


def _free_path(root: Path, rel: Path, owned: set[str]) -> Path:
    """目标位置被非同步产生的文件占用时，改用「名称 (Moodle).ext」避免覆盖用户文件。"""
    if not (root / rel).exists() or rel.as_posix() in owned:
        return rel
    candidate = rel.with_name(f"{rel.stem} (Moodle){rel.suffix}")
    n = 2
    while (root / candidate).exists() and candidate.as_posix() not in owned:
        candidate = rel.with_name(f"{rel.stem} (Moodle {n}){rel.suffix}")
        n += 1
    return candidate


async def sync_course(
    moodle: Moodle,
    course: Course,
    exts: set[str] | None = None,
    dry_run: bool = False,
    base: Path = DOWNLOAD_DIR,
    only_cmid: int | None = None,
) -> SyncReport:
    """同步整门课；only_cmid 指定时只处理该活动（任意模块类型）里的文件。"""
    root = course_dir(course, base)
    report = SyncReport(course=course, root=root, dry_run=dry_run)
    sections = await moodle.material_contents(course.id, only_cmid)
    for section in sections:
        for module in section.get("modules", []):
            if only_cmid is not None and int(module["id"]) != only_cmid:
                continue
            if not module.get("uservisible", True) or (only_cmid is None and module.get("modname") not in SYNC_MODULES):
                continue
            content = module.get("_tabbed")
            if content and not content.complete:
                report.warnings.append(f"cmid={module['id']}：{content.warning} 未找到课件不代表老师未上传。")
            for item in module.get("contents") or []:
                if not item.get("_metadata_unknown") or not _match_ext(item.get("filename") or "", exts):
                    continue
                try:
                    item.update(await moodle.client.file_metadata(item["fileurl"]))
                    item["_metadata_unknown"] = not all(k in item for k in ("filesize", "timemodified"))
                    if item["_metadata_unknown"]:
                        report.warnings.append(f"{item.get('filename')}：响应头缺少大小或更新时间，下次同步将重新核验。")
                except TokenInvalid:
                    raise
                except (MoodleError, ValueError):
                    report.warnings.append(f"{item.get('filename')}：未取得文件元数据，仍会尝试下载，下次同步将重新核验。")
    if only_cmid is None:
        remote = plan_files(sections, exts)
    else:
        every = {m.get("modname", "") for sec in sections for m in sec.get("modules", [])}
        remote = [f for f in plan_files(sections, exts, every) if f.cmid == only_cmid]
    manifest = load_manifest(root)
    owned = {e.get("path", "") for e in manifest.values()}

    todo: list[tuple[RemoteFile, Path]] = []
    for f in remote:
        entry = manifest.get(f.key)
        if _is_current(entry, f, root):
            report.unchanged += 1
            continue
        if entry and entry.get("path"):
            rel = Path(entry["path"])
        else:
            # 本地已有同名同大小文件（例如之前手动下载过）时直接登记，不重复下载
            local = root / f.rel
            if f.metadata_known and f.size and local.is_file() and local.stat().st_size == f.size:
                manifest[f.key] = {"path": f.rel.as_posix(), "modified": f.modified, "size": f.size}
                owned.add(f.rel.as_posix())
                report.unchanged += 1
                continue
            rel = _free_path(root, f.rel, owned)
        owned.add(rel.as_posix())
        todo.append((f, rel))
        (report.updated if entry else report.new).append(f)
    report.files = {f.key: f.rel for f in remote}

    if dry_run or not todo:
        if not dry_run and manifest:
            save_manifest(root, course, manifest)
        return report

    sem = asyncio.Semaphore(CONCURRENCY)

    async def fetch(f: RemoteFile, rel: Path) -> None:
        async with sem:
            try:
                await moodle.client.download(f.url, root / rel)
                if f.modified:
                    os.utime(root / rel, (f.modified, f.modified))
                manifest[f.key] = {"path": rel.as_posix(), "modified": f.modified, "size": (root / rel).stat().st_size}
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响整门课
                report.failed.append((f, str(exc)))

    await asyncio.gather(*(fetch(f, rel) for f, rel in todo))
    failed = {f.key for f, _ in report.failed}
    report.new = [f for f in report.new if f.key not in failed]
    report.updated = [f for f in report.updated if f.key not in failed]
    save_manifest(root, course, manifest)
    return report
