"""The coach's Markdown becomes HTML, and the model's output is never trusted."""

import re
from html.parser import HTMLParser

import pytest

from markup import ALLOWED_TAGS, render_markdown

DOC = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"


def test_lists_emphasis_and_code_render():
    html = str(
        render_markdown(
            "**Start here**\n\n- one\n- two\n\n1. first\n2. second\n\n`M3 bolt`\n\n"
            "```\nline 1\nline 2\n```"
        )
    )

    assert "<strong>Start here</strong>" in html
    assert "<ul>" in html and "<li>one</li>" in html
    assert "<ol>" in html and "<li>first</li>" in html
    assert "<code>M3 bolt</code>" in html
    assert "<pre><code>line 1\nline 2\n</code></pre>" in html


def test_google_links_open_in_a_new_tab_safely():
    html = str(render_markdown(f"See [the notes]({DOC}) and {DOC}."))

    assert html.count('target="_blank"') == 2
    assert html.count('rel="noopener noreferrer"') == 2
    assert f'<a href="{DOC}"' in html
    assert f">{DOC}</a>." in html  # the sentence's period is not part of the link


class _Elements(HTMLParser):
    """Every real element in the output (escaped text is not an element)."""

    def __init__(self):
        super().__init__()
        self.found: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.found.append((tag, dict(attrs)))


def elements(html: str):
    parser = _Elements()
    parser.feed(html)
    return parser.found


@pytest.mark.parametrize(
    "markdown",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<iframe src='https://evil.example'></iframe>",
        "<a href='javascript:alert(1)' onclick='x()'>hi</a>",
        "<style>body{display:none}</style>",
        "[click](javascript:alert(1))",
        "[click](data:text/html;base64,PHNjcmlwdD4=)",
        "![pic](https://evil.example/p.png)",
        "[evil](https://evil.example/x)",
        "https://evil.example/plain",
        "[lookalike](https://docs.google.com.evil.example/x)",
        "[http](http://docs.google.com/document/d/1AbCdEfGhIjKlMnOp)",
        "# Heading\n\n> quote\n\n---",
    ],
)
def test_hostile_markdown_is_neutralised(markdown):
    found = elements(str(render_markdown(markdown)))

    for tag, attrs in found:
        assert tag in ALLOWED_TAGS, f"unexpected <{tag}>"
        if tag == "a":
            assert set(attrs) == {"href", "target", "rel"}, attrs
            assert re.fullmatch(
                r"https://(docs|drive)\.google\.com/\S*", attrs["href"]
            ), attrs
        else:
            assert attrs == {}, f"<{tag}> kept attributes {attrs}"


def test_only_allowlisted_tags_ever_come_out():
    hostile = (
        "# t\n\n<b>raw</b> <div>x</div> <svg onload=1></svg>\n\n"
        f"- [a]({DOC})\n- `c`\n\n```\n<script>1</script>\n```\n\n**s** *e*\n"
    )

    tags = set(re.findall(r"</?([a-z0-9]+)", str(render_markdown(hostile))))

    assert tags <= ALLOWED_TAGS


def test_raw_html_is_shown_as_text_not_run():
    html = str(render_markdown("<b>not bold</b>"))

    assert "&lt;b&gt;not bold&lt;/b&gt;" in html


def test_empty_text_is_fine():
    assert str(render_markdown("")) == ""
