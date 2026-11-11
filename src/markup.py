"""Turn the coach's Markdown into safe HTML.

The model's output is untrusted. Two steps, both required:

1. markdown-it-py converts Markdown to HTML with raw HTML turned off, so a
   `<script>` in the answer is escaped as text, never passed through.
2. nh3 sanitizes the result against an allowlist, as a second wall in case the
   first one ever has a gap.

Only https links to Google Docs/Drive stay clickable (the coach cites Drive
documents, and nothing else should be a link a member might trust and click). The one
exception is this app's own decision pages, a path like /decisions/12 that the coach
got from its find_decisions tool; nothing else on this site can be linked.
"""

import re
from urllib.parse import urlsplit

import nh3
from markdown_it import MarkdownIt
from markupsafe import Markup

ALLOWED_TAGS = {"p", "ul", "ol", "li", "strong", "em", "code", "pre", "a"}
ALLOWED_ATTRIBUTES = {"a": {"href"}}
ALLOWED_LINK_HOSTS = {"docs.google.com", "drive.google.com"}


_DECISION_PATH = re.compile(r"/decisions/\d+")
# A bare /decisions/12 in the text (not already inside a link, path or code span).
_BARE_DECISION_PATH = re.compile(r"(?<![\w/(\[`])/decisions/\d+(?![\w/-])")


def _is_allowed_link(url: str) -> bool:
    if _DECISION_PATH.fullmatch(url):
        return True
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "") in ALLOWED_LINK_HOSTS


_markdown = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("linkify")
# Links to anywhere else are never created: the text stays as plain text.
_markdown.validateLink = _is_allowed_link


def _filter_attribute(tag: str, attribute: str, value: str) -> str | None:
    """nh3 hook (second wall): keep an href only if it points at Google Docs/Drive."""
    if tag == "a" and attribute == "href":
        return value if _is_allowed_link(value) else None
    return value


def render_markdown(text: str) -> Markup:
    """Markdown in, sanitized HTML out. Safe to put in a template unescaped."""
    text = _BARE_DECISION_PATH.sub(lambda m: f"[{m.group(0)}]({m.group(0)})", text or "")
    html = _markdown.render(text)
    clean = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_filter_attribute,
        url_schemes={"https"},
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
        strip_comments=True,
    )
    return Markup(clean)
