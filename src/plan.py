"""The shape of a "Plan my next steps" answer. Pure: text in, parts out. No model, no database.

The coach writes five labelled parts (`prompts/coach_plan.md`); the app adds the sixth, "Then",
from the task's steps (`steps.then_text`). The page shows them as sections, and the one part
that holds a ready-to-paste prompt gets a box with a Copy button. A reply that doesn't have
the parts is shown as an ordinary message.
"""

import re

# (key, label as written): the order of a plan.
SECTIONS = (
    ("found", "What I found"),
    ("missing", "What's missing"),
    ("now", "Do this now"),
    ("help", "If you need more help"),
    ("outputs", "You should end up with"),
    ("then", "Then"),
)
LABELS = {key: label for key, label in SECTIONS}
_BY_WORDS = {label.casefold(): key for key, label in SECTIONS}
MIN_PARTS = 3  # fewer labelled parts than this and it isn't a plan

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'"})
_FENCE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.DOTALL)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MARKS = re.compile(r"[*_`#>]")

# Shown next to the Copy button: the prompt quotes the team Drive.
COPY_WARNING = (
    "This text includes information from the team Drive. "
    "Paste it only into tools your team allows."
)


def _key_for(words: str) -> str | None:
    return _BY_WORDS.get(" ".join(words.translate(_APOSTROPHES).casefold().split()).strip(":"))


def heading(line: str) -> tuple[str, str] | None:
    """(section key, the rest of the line) if the line opens a part: a label in bold
    (`**Do this now**`, `**Then:** ...`), a Markdown heading, or "Then: ..."."""
    text = line.strip().lstrip("#").strip()
    for mark in ("**", "__"):
        if text.startswith(mark):
            end = text.find(mark, len(mark))
            if end != -1:
                key = _key_for(text[len(mark) : end])
                if key:
                    return key, text[end + len(mark) :].lstrip(":").strip()
    if line.strip().startswith("#"):
        key = _key_for(text)
        if key:
            return key, ""
    head, colon, rest = text.partition(":")
    if colon:
        key = _key_for(head)
        if key:
            return key, rest.strip()
    return None


def parse(content: str) -> list[dict]:
    """The parts of a plan, in the order written: {"key", "title", "text", "prompt"}. `text` is
    Markdown; `prompt` is the fenced block of "If you need more help" (else None). Empty if
    the content isn't a plan."""
    found: list[dict] = []
    for line in (content or "").splitlines():
        opened = heading(line)
        if opened:
            key, rest = opened
            found.append({"key": key, "title": LABELS[key], "lines": [rest] if rest else []})
        elif found:
            found[-1]["lines"].append(line)
    parts = []
    for part in found:
        text = "\n".join(part["lines"]).strip()
        prompt = None
        if part["key"] == "help":
            fenced = _FENCE.search(text)
            if fenced:
                prompt = fenced.group(1).strip() or None
                text = (text[: fenced.start()] + text[fenced.end() :]).strip()
            elif text:  # no fence: the whole part is the prompt
                prompt, text = text, ""
        parts.append({"key": part["key"], "title": part["title"], "text": text, "prompt": prompt})
    return parts if len({p["key"] for p in parts}) >= MIN_PARTS else []


def _section(content: str, key: str) -> str:
    for part in parse(content):
        if part["key"] == key:
            return part["text"]
    return ""


def has_numbers_in_outputs(content: str) -> bool:
    """Whether "You should end up with" holds a digit (outside links): that part describes
    outputs and states no values."""
    text = _URL.sub("", _LINK.sub(r"\1", _section(content, "outputs")))
    return bool(re.search(r"\d", text))


def do_this_now(content: str) -> str:
    """The "Do this now" action as plain text ("" if there is none)."""
    text = _LINK.sub(r"\1", _section(content, "now"))
    return " ".join(_MARKS.sub("", text).split())
