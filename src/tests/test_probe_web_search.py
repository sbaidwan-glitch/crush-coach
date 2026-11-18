"""The web-search probe script: how it reads a reply's citations."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_web_search as probe


def test_url_citations_are_read_from_a_reply():
    note = SimpleNamespace(url_citation=SimpleNamespace(url="https://example.test/a"))
    message = SimpleNamespace(annotations=[note])

    assert probe.citations(message) == ["https://example.test/a"]


def test_a_reply_without_annotations_has_no_citations():
    assert probe.citations(SimpleNamespace(content="from memory")) == []
    assert probe.citations(SimpleNamespace(annotations=None)) == []
