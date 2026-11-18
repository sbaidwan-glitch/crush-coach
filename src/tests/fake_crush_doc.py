"""A fake Google Doc you can write to: the Docs API calls drive_write makes, nothing more.

Everything in it is made up (the document id is a placeholder, never a real one). It keeps
each tab as one string of characters with the style of each character, and applies
`batchUpdate` requests with the same index rules as Docs (the body starts at index 1, text
can be inserted anywhere before a tab's final newline, new text inherits the look of what
is next to it). That makes placement, headings and styling checkable, and lets a test
prove an edit only ever added text.
"""

import copy
from dataclasses import dataclass, field
from typing import ClassVar

from fake_google import _Request, http_error

DOC_ID = "docCrushLog"  # a placeholder: tests set DECISIONS_DOC_ID to it


@dataclass
class _Char:
    ch: str
    bold: bool = False
    link: str | None = None
    style: str = "NORMAL_TEXT"  # paragraph style, meaningful on a newline


@dataclass
class _Tab:
    tab_id: str
    title: str
    chars: list[_Char] = field(default_factory=list)
    children: list["_Tab"] = field(default_factory=list)

    def text(self) -> str:
        return "".join(c.ch for c in self.chars)


class FakeCrushDoc:
    """`docs.documents().get(...)` / `.batchUpdate(...)` over a made-up document."""

    ALLOWED: ClassVar[set[str]] = {
        "insertText",
        "updateParagraphStyle",
        "updateTextStyle",
        "deleteParagraphBullets",
    }

    def __init__(self, *tab_titles: str):
        self.tabs: list[_Tab] = []
        self.revision = 1
        self.sent: list[list[dict]] = []  # every batchUpdate's requests
        self.fail_get: Exception | None = None
        self.fail_update: Exception | None = None
        self.before_update = None  # called with the doc just before a batchUpdate applies
        self.gets = 0
        for i, title in enumerate(tab_titles):
            self.add_tab(title, tab_id=f"t.{i}")

    # --- building and reading it in tests ---

    def add_tab(self, title: str, text: str = "", tab_id: str | None = None) -> _Tab:
        tab = _Tab(tab_id or f"t.{len(self.tabs)}", title)
        tab.chars = [_Char(c) for c in text + "\n"]
        self.tabs.append(tab)
        return tab

    def tab(self, title: str) -> _Tab:
        return next(t for t in self._all() if t.title == title)

    def _all(self) -> list[_Tab]:
        found: list[_Tab] = []

        def walk(tabs):
            for t in tabs:
                found.append(t)
                walk(t.children)

        walk(self.tabs)
        return found

    def prepend(self, title: str, line: str) -> None:
        """Someone else types a line at the top of a tab (the document's revision moves on)."""
        self.tab(title).chars[:0] = [_Char(c) for c in line + "\n"]
        self.revision += 1

    def set_style(self, title: str, line: str, style: str) -> None:
        """Give the paragraph with this text a named style (e.g. HEADING_3)."""
        tab = self.tab(title)
        text = tab.text()
        start = text.index(line + "\n")
        tab.chars[start + len(line)].style = style

    def lines(self, title: str) -> list[str]:
        return self.tab(title).text().rstrip("\n").split("\n")

    def bold_text(self, title: str) -> str:
        return "".join(c.ch for c in self.tab(title).chars if c.bold)

    def links(self, title: str) -> dict[str, str]:
        """link url -> the text carrying it"""
        found: dict[str, str] = {}
        for c in self.tab(title).chars:
            if c.link:
                found[c.link] = found.get(c.link, "") + c.ch
        return found

    def make_bold(self, title: str, line: str) -> None:
        tab = self.tab(title)
        start = tab.text().index(line + "\n")
        for c in tab.chars[start : start + len(line)]:
            c.bold = True

    def style_of(self, title: str, line: str) -> str:
        tab = self.tab(title)
        text = tab.text()
        start = text.index(line + "\n")
        return tab.chars[start + len(line)].style

    def snapshot(self) -> dict[str, str]:
        return {t.title: t.text() for t in self._all()}

    # --- the API ---

    def documents(self):
        return _Documents(self)

    def _resource(self) -> dict:
        def tab_resource(tab: _Tab) -> dict:
            content = [{"sectionBreak": {}, "startIndex": 0, "endIndex": 1}]
            index = 1
            paragraph: list[_Char] = []
            for c in tab.chars:
                paragraph.append(c)
                if c.ch != "\n":
                    continue
                runs: list[dict] = []
                for ch in paragraph:
                    key = (ch.bold, ch.link)
                    if runs and runs[-1]["key"] == key:
                        runs[-1]["text"] += ch.ch
                    else:
                        runs.append({"key": key, "text": ch.ch})
                size = len(paragraph)
                content.append(
                    {
                        "startIndex": index,
                        "endIndex": index + size,
                        "paragraph": {
                            "paragraphStyle": {"namedStyleType": c.style},
                            "elements": [
                                {"textRun": {"content": r["text"]}} for r in runs
                            ],
                        },
                    }
                )
                index += size
                paragraph = []
            return {
                "tabProperties": {"tabId": tab.tab_id, "title": tab.title},
                "documentTab": {"body": {"content": content}},
                "childTabs": [tab_resource(child) for child in tab.children],
            }

        return {
            "documentId": DOC_ID,
            "revisionId": f"rev{self.revision}",
            "tabs": [tab_resource(t) for t in self.tabs],
        }

    def _apply(self, requests: list[dict]) -> None:
        for request in requests:
            (name, body), = request.items()
            if name not in self.ALLOWED:
                raise http_error(400, f"unsupported request {name}")
            getattr(self, f"_do_{name}")(body)

    def _find(self, tab_id: str) -> _Tab:
        for tab in self._all():
            if tab.tab_id == tab_id:
                return tab
        raise http_error(400, "no such tab")

    def _do_insertText(self, body: dict) -> None:
        tab = self._find(body["location"]["tabId"])
        index = body["location"]["index"]
        at = index - 1
        if not 0 <= at <= len(tab.chars) - 1:  # never after the final newline
            raise http_error(400, f"index {index} is outside the tab")
        # The paragraph being split: new newlines take its style.
        end = next(i for i in range(at, len(tab.chars)) if tab.chars[i].ch == "\n")
        paragraph_style = tab.chars[end].style
        # Inserted text takes the look of the text it lands next to: the text before it, or
        # (at the start of a paragraph) the text after it, e.g. a heading's.
        if at > 0 and tab.chars[at - 1].ch != "\n":
            before = tab.chars[at - 1]
        else:
            before = tab.chars[at] if tab.chars[at].ch != "\n" else None
        new = [
            _Char(
                ch,
                bold=before.bold if before and ch != "\n" else False,
                link=before.link if before and ch != "\n" else None,
                style=paragraph_style,
            )
            for ch in body["text"]
        ]
        tab.chars[at:at] = new

    def _paragraph_bounds(self, tab: _Tab):
        start = 0
        for i, c in enumerate(tab.chars):
            if c.ch == "\n":
                yield start, i  # [start, i] inclusive of the newline
                start = i + 1

    def _do_updateParagraphStyle(self, body: dict) -> None:
        tab = self._find(body["range"]["tabId"])
        lo, hi = body["range"]["startIndex"] - 1, body["range"]["endIndex"] - 1
        assert body["fields"] == "namedStyleType"
        for start, newline in self._paragraph_bounds(tab):
            if start < hi and newline + 1 > lo:
                tab.chars[newline].style = body["paragraphStyle"]["namedStyleType"]

    def _do_deleteParagraphBullets(self, body: dict) -> None:
        self._find(body["range"]["tabId"])

    def _do_updateTextStyle(self, body: dict) -> None:
        tab = self._find(body["range"]["tabId"])
        lo, hi = body["range"]["startIndex"] - 1, body["range"]["endIndex"] - 1
        if not 0 <= lo <= hi <= len(tab.chars):
            raise http_error(400, "range outside the tab")
        fields = body["fields"].split(",")
        style = body["textStyle"]
        for c in tab.chars[lo:hi]:
            if "bold" in fields:
                c.bold = bool(style.get("bold", False))
            if "link" in fields:
                c.link = (style.get("link") or {}).get("url")


class _Documents:
    def __init__(self, fake: FakeCrushDoc):
        self.fake = fake

    def get(self, documentId, includeTabsContent=False, **kw):
        fake = self.fake
        assert includeTabsContent is True

        def run():
            fake.gets += 1
            if fake.fail_get:
                raise fake.fail_get
            assert documentId == DOC_ID
            return fake._resource()

        return _Request(run)

    def batchUpdate(self, documentId, body, **kw):
        fake = self.fake

        def run():
            if fake.before_update:
                fake.before_update(fake)
            if fake.fail_update:
                raise fake.fail_update
            assert documentId == DOC_ID
            required = (body.get("writeControl") or {}).get("requiredRevisionId")
            if required and required != f"rev{fake.revision}":
                raise http_error(400, "The required revision ID does not match")
            saved = copy.deepcopy(fake.tabs)
            try:
                fake._apply(body["requests"])
            except Exception:
                fake.tabs = saved  # all or nothing, like the real API
                raise
            fake.sent.append(body["requests"])
            fake.revision += 1
            return {"documentId": documentId, "replies": [{} for _ in body["requests"]]}

        return _Request(run)
