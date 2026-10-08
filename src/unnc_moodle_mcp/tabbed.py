"""分页内容解析：保留所有分页，包括网页中未选中的分页。"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

from .client import ws_file_url
from .config import MOODLE_URL
from .textutil import html_to_text, safe_name

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


@dataclass
class Tab:
    id: str
    title: str
    html: str


@dataclass
class TabbedContent:
    tabs: list[Tab] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    source: str = ""
    warning: str = ""
    complete: bool = True


class _TabsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.depth = 0
        self.headers: dict[str, str] = {}
        self.panels: dict[str, str] = {}
        self.capture: tuple[str, str, int] | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if self.capture:
            self.parts.append(self.get_starttag_text())
        elif a.get("role") == "tab":
            target = a.get("aria-controls") or a.get("data-bs-target") or a.get("href") or ""
            self.capture = ("header", target.lstrip("#"), self.depth)
            self.parts = []
        elif a.get("role") == "tabpanel":
            self.capture = ("panel", a.get("id") or "", self.depth)
            self.parts = []
        if tag not in _VOID:
            self.depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.capture:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID:
            self.depth = max(0, self.depth - 1)
        if not self.capture:
            return
        kind, key, depth = self.capture
        if self.depth == depth:
            value = "".join(self.parts)
            (self.headers if kind == "header" else self.panels)[key] = value
            self.capture = None
        else:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")


class _FileLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self.embedded = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in {"iframe", "object", "embed"}:
            self.embedded = True
        if tag == "a" and a.get("href"):
            self.urls.append(a["href"])


def content_from_tabs(tabs: list[Tab], source: str, complete: bool = True, warning: str = "") -> TabbedContent:
    """正文链接只转换本站文件，不访问外站、不把凭据查询参数带到输出中。"""
    files: list[dict] = []
    seen: set[str] = set()
    for tab in tabs:
        parser = _FileLinks()
        parser.feed(tab.html)
        if parser.embedded:
            complete = False
            warning = "存在嵌入页面，未读取其内部内容。" + warning
        for href in parser.urls:
            try:
                url = ws_file_url(urljoin(MOODLE_URL + "/", href))
            except ValueError:
                continue
            identity = urlparse(url).path
            if identity in seen:
                continue
            seen.add(identity)
            name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
            if not name:
                continue
            files.append({
                "type": "file", "filename": name, "fileurl": url,
                "filepath": f"/{safe_name(tab.title)}/", "_file_key": identity,
                "_metadata_unknown": True,
            })
    return TabbedContent(tabs, files, source, warning, complete)


def parse_mobile_content(data: dict) -> TabbedContent:
    parser = _TabsParser()
    for template in data.get("templates") or []:
        parser.feed(template.get("html") or "")
    parser.close()
    if not parser.headers or set(parser.headers) != set(parser.panels):
        raise ValueError("移动端响应未包含完整的分页结构")
    tabs = [Tab(key, html_to_text(title), parser.panels[key]) for key, title in parser.headers.items()]
    result = content_from_tabs(tabs, "官方移动端 API")
    # API 提供的文件元数据优先于正文中只有 URL 的文件链接。
    metadata = {}
    for item in data.get("files") or []:
        try:
            url = ws_file_url(item.get("fileurl") or "")
        except ValueError:
            continue
        metadata[url] = item
    for item in result.files:
        remote = metadata.get(item["fileurl"], {})
        for key in ("filesize", "timemodified"):
            if remote.get(key) is not None:
                item[key] = remote[key]
        item["_metadata_unknown"] = not all(key in item for key in ("filesize", "timemodified"))
    if data.get("warnings"):
        result.complete = False
        result.warning += "移动端 API 返回了读取警告。"
    return result
