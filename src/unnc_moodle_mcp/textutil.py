"""文本工具：HTML 转纯文本、时间格式化、文件名清洗。"""

from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser

from .config import TZ

_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "table", "tr", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr", "figure",
}
_SKIP_TAGS = {"script", "style", "noscript", "template"}
_WEEKDAYS = "一二三四五六日"


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._href = href if href.startswith(("http://", "https://", "mailto:")) else None
            self._link_text = []
        elif tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                self.parts.append(f"[图片：{alt}]")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href:
            text = "".join(self._link_text).strip()
            if self._href not in text:
                self.parts.append(f"（{self._href}）")
            self._href = None
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data)


def html_to_text(html: str | None) -> str:
    """把 Moodle 返回的 HTML 片段转为可读纯文本，保留链接地址与段落。"""
    if not html:
        return ""
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def fmt_time(ts: int | float | None) -> str:
    """Unix 时间戳 → UTC+8 的「2026-10-08 14:00（周三）」。"""
    if not ts:
        return "无"
    dt = datetime.fromtimestamp(int(ts), tz=TZ)
    return f"{dt:%Y-%m-%d %H:%M}（周{_WEEKDAYS[dt.weekday()]}）"


def fmt_size(size: int | None) -> str:
    size = int(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name: str, limit: int = 120) -> str:
    """清洗为 macOS/Windows 都可用的单级文件或目录名。"""
    cleaned = _UNSAFE.sub("_", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if cleaned in ("", ".", ".."):
        cleaned = "未命名"
    if len(cleaned) > limit:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext) <= 8:
            cleaned = stem[: limit - len(ext) - 1].rstrip() + "." + ext
        else:
            cleaned = cleaned[:limit].rstrip()
    return cleaned


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n…（已截断，原文共 {len(text)} 字符）"
