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


class _Reply:
    def __init__(self, message):
        self._message = message

    def model_dump(self):
        return {"choices": [{"message": dict(self._message)}], "id": "x"}


def test_urls_are_found_wherever_the_proxy_put_them_but_not_in_the_answer_text():
    reply = _Reply(
        {
            "role": "assistant",
            "content": "see https://in-the-text.test/a",
            "provider_specific_fields": {"citations": ["https://found.test/page"]},
        }
    )

    assert probe.urls_anywhere(reply) == ["https://found.test/page"]
    assert probe.extra_fields(reply) == ["message.provider_specific_fields"]


def test_a_plain_reply_has_no_extra_fields_or_urls():
    reply = _Reply({"role": "assistant", "content": "from memory", "annotations": []})

    assert probe.urls_anywhere(reply) == [] and probe.extra_fields(reply) == []
