"""The DB-free part of an answer: events as it is written, and the finished checks."""

import json
import threading

import fake_openai as fake
import pytest
from index_helpers import add_file

import answering
import llm
import retrieval
from models import Task

LINK = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"
DOC_ID = "1AbCdEfGhIjKlMnOp"
TASK = {
    "Code": "M04",
    "Title": "Finish enclosure wall panels",
    "Subsystem": "Enclosure",
}


@pytest.fixture
def found(session):
    add_file(
        session,
        "Latch design notes",
        chunks=["Use the two-point latch."],
        drive_id=DOC_ID,
        link=LINK,
    )
    task = Task(
        code="M04",
        title="Panels",
        subsystem="Enclosure",
        task_type="Fabricate",
        stage="x",
    )
    return retrieval.retrieve(session, task, "latch")


def start(monkeypatch, found, *script, **kwargs):
    client = fake.FakeClient(script)
    monkeypatch.setattr(llm, "get_client", lambda: client)
    records = []
    run = answering.AnswerRun(
        TASK,
        found,
        "Which latch?",
        [],
        today="2026-11-11",
        record=records.append,
        **kwargs,
    )
    return run, records, client


def test_events_end_with_an_answer_that_has_its_cited_sources(monkeypatch, found):
    run, records, _ = start(
        monkeypatch, found, fake.reply(f"Two-point latch ({LINK}).")
    )

    events = list(run)

    assert events[-1]["type"] == "text" and "Two-point latch" in events[-1]["text"]
    answer = run.answer
    assert answer.text == f"Two-point latch ({LINK})."
    assert [s["title"] for s in answer.sources] == ["Latch design notes"]
    assert answer.tool_calls == 0 and answer.cut_short is False
    assert answer.seconds >= 0 and answer.first_text_seconds is not None
    assert len(records) == 1


def test_the_prompt_has_today_and_the_facts_in_the_system_message(monkeypatch, found):
    found.facts = ["Current season is 2026-27"]
    run, _, client = start(monkeypatch, found, fake.reply("ok"))

    run.run()

    system = client.requests[0]["messages"][0]["content"]
    assert system.startswith(llm.system_prompt())
    assert "## Today\n2026-11-11" in system
    assert "- Current season is 2026-27" in system


def test_text_events_are_throttled(monkeypatch, found):
    monkeypatch.setattr(answering, "TEXT_EVERY_SECONDS", 10.0)
    run, _, _ = start(monkeypatch, found, fake.reply("one two three four five"))

    events = [e for e in run if e["type"] == "text"]

    assert len(events) == 2  # the first piece, and the final flush


def test_an_invented_link_never_reaches_the_page_even_while_streaming(
    monkeypatch, found
):
    invented = "https://docs.google.com/document/d/1MadeUpMadeUpMadeUp/edit"
    run, _, _ = start(monkeypatch, found, fake.reply(f"See {invented} or {LINK}."))
    monkeypatch.setattr(answering, "TEXT_EVERY_SECONDS", 0.0)

    events = list(run)

    assert all(invented not in e.get("text", "") for e in events)
    assert run.answer.ungrounded_links is True
    assert [s["link"] for s in run.answer.sources] == [LINK]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("See https://docs.google.com/document/d/1Ab", "See "),
        ("See (https://docs.google.com/doc", "See ("),
        ("See [https://docs.goo", "See ["),
        (
            "See https://docs.google.com/document/d/1Ab ",
            "See https://docs.google.com/document/d/1Ab ",
        ),
        ("No link at all", "No link at all"),
        ("Ends with http", "Ends with "),
        ("", ""),
    ],
)
def test_a_half_written_link_is_held_back(text, expected):
    assert answering.hold_back_partial_url(text) == expected


def test_notes_say_what_went_wrong():
    answer = llm.CoachAnswer(
        text="x",
        drive_failed=True,
        decisions_failed=True,
        ungrounded_links=True,
        unsourced_numbers=True,
        cut_short=True,
    )

    notes = answering.notes_for(answer)

    assert len(notes) == 5
    assert any("stopped early" in n for n in notes)
    assert any("Reading a Google Doc live failed" in n for n in notes)
    assert answering.notes_for(llm.CoachAnswer(text="fine")) == []


def test_an_empty_model_reply_is_an_error_never_a_saved_answer(monkeypatch, found):
    """The old placeholder ("I couldn't put an answer together") was saved as the coach's
    answer, with no Retry button. Now it is a failure, which the page shows with one."""
    run, calls, _ = start(monkeypatch, found, fake.reply(""), fake.reply(""))

    with pytest.raises(llm.LLMError) as caught:
        run.run()

    assert caught.value.kind == "empty"
    assert "couldn't put an answer together" not in caught.value.message
    assert [c.status for c in calls] == ["empty", "empty"]


def test_the_live_read_returns_the_text_untrusted_and_truncated(monkeypatch, found):
    long_text = "word " * 10_000
    run, _, client = start(
        monkeypatch,
        found,
        fake.tool_request(("r1", "read_file", json.dumps({"file_id": DOC_ID}))),
        fake.reply("From the doc."),
        read_doc=lambda file_id: long_text,
    )

    answer = run.run()

    assert answer.tool_calls == 1 and answer.drive_failed is False
    result = json.loads(client.requests[1]["messages"][-1]["content"])
    assert len(result["document_text_untrusted"]) == llm.MAX_DOC_CHARS
    assert result["truncated"] is True


def test_the_live_read_only_opens_files_the_model_was_shown():
    read = answering.live_reader({DOC_ID}, lambda file_id: "text")

    assert json.loads(read(DOC_ID, 5.0))["document_text_untrusted"] == "text"
    with pytest.raises(ValueError):
        read("someOtherFile123", 5.0)


def test_a_live_read_gives_up_when_the_time_is_gone():
    release = threading.Event()
    read = answering.live_reader({DOC_ID}, lambda file_id: release.wait(5))

    try:
        with pytest.raises(TimeoutError):
            read(DOC_ID, 0.05)
    finally:
        release.set()


# --- an answer that hit the output cap ends at its last full sentence ---


def test_an_answer_cut_by_the_cap_ends_cleanly_and_says_it_was_shortened(
    monkeypatch, found
):
    cut = "The lid uses the two-point latch. It seals evenly. Next step: open the dra"
    run, _, _ = start(monkeypatch, found, fake.reply(cut, finish_reason="length"))

    answer = run.run()

    assert answer.text == "The lid uses the two-point latch. It seals evenly."
    assert answer.shortened is True
    assert answering.NOTE_SHORTENED in answering.notes_for(answer)


def test_a_complete_answer_is_left_alone(monkeypatch, found):
    text = "The lid uses the two-point latch.\n\nNext step: check the holes"
    run, _, _ = start(monkeypatch, found, fake.reply(text))

    answer = run.run()

    assert answer.text == text and answer.shortened is False
    assert answering.notes_for(answer) == []


@pytest.mark.parametrize(
    ("cut", "clean"),
    [
        (
            (
                "Use the latch in [Draw Latch](https://drive.google.com/file/d/ABC/view). "
                "Next step: open it and che"
            ),
            "Use the latch in [Draw Latch](https://drive.google.com/file/d/ABC/view).",
        ),
        ("1. Measure it.\n2. Then comp", "1. Measure it."),
        ("Use e.g. a latch. It seals. The other", "Use e.g. a latch. It seals."),
        (
            "**Likely current: .030.** Older says .050. A lead",
            "**Likely current: .030.** Older says .050.",
        ),
        ("A sentence. **Bold that never clo", "A sentence."),
    ],
)
def test_clean_end_never_leaves_half_a_link_a_list_number_or_open_bold(cut, clean):
    assert answering.clean_end(cut) == clean


def test_with_no_full_sentence_the_text_stops_at_a_whole_word():
    cut = answering.clean_end("The lid uses the draw latch in [Exa")

    assert cut == "The lid uses the draw latch…"


def test_text_cut_at_the_time_limit_is_also_cleaned(monkeypatch, found):
    run, _, _ = start(
        monkeypatch,
        found,
        fake.reply("It is the draw latch. The mount", then_raise=fake.timed_out()),
    )

    answer = run.run()

    assert answer.cut_short is True and answer.text == "It is the draw latch."


# --- [S3] becomes a link ---


def test_a_numbered_citation_becomes_a_link_to_that_source(monkeypatch, found):
    run, _, _ = start(
        monkeypatch,
        found,
        fake.reply("Use the two-point latch [S1]. It seals evenly [S9]."),
    )

    answer = run.run()

    assert f"[Latch design notes]({LINK})" in answer.text
    assert "[S9]" in answer.text  # no such source: left as written, nothing invented
    assert [s["title"] for s in answer.sources] == ["Latch design notes"]


def test_a_real_link_next_to_its_number_is_left_alone():
    text = "See [Latch design notes](https://x.test/a) [S1]."
    hit = type(
        "Hit", (), {"name": "Latch design notes", "web_link": "https://x.test/a"}
    )

    assert (
        answering.link_source_numbers(text, {"S1": hit}).count("https://x.test/a") == 2
    )
    already = "[S1](https://z.test)"  # already a Markdown link: not touched
    assert answering.link_source_numbers(already, {"S1": hit}) == already


def test_the_streamed_text_shows_numbered_citations_as_links_too(monkeypatch, found):
    run, _, _ = start(monkeypatch, found, fake.reply("Use the two-point latch [S1]."))

    events = list(run)

    texts = [e["text"] for e in events if e["type"] == "text"]
    assert f"[Latch design notes]({LINK})" in texts[-1]


# --- recommendation mode ---


@pytest.fixture
def kit(session, found):
    import contextlib

    import recommend

    add_file(
        session,
        "Parker O-Ring Handbook.pdf",
        kind="pdf",
        folder="MuddSub / Library / Textbooks",
        chunks=["Static face seal gland squeeze should be 15 to 30 percent."],
        drive_id="handbook0001",
        link="https://drive.google.com/file/d/1HandbookHandbookHandbook/view",
    )
    return recommend.RecommendKit(found, lambda: contextlib.nullcontext(session))


def start_recommend(monkeypatch, found, kit, *script):
    client = fake.FakeClient(script)
    monkeypatch.setattr(llm, "get_client", lambda: client)
    records = []
    run = answering.AnswerRun(
        TASK,
        found,
        "Give me your recommendation.",
        [],
        today="2026-11-15",
        record=records.append,
        recommend=kit,
    )
    return run, records, client


def test_a_recommendation_searches_calculates_and_cites_what_it_found(
    monkeypatch, found, kit
):
    run, records, client = start_recommend(
        monkeypatch,
        found,
        kit,
        fake.tool_request(("s1", "search_references", '{"query": "gasket squeeze"}')),
        fake.tool_request(("c1", "calculate", '{"expression": "0.125 * 0.2"}')),
        fake.reply("**Recommendation:** squeeze 20 percent [R1]. Gland depth 0.025."),
    )

    answer = run.run()

    assert (
        "[Parker O-Ring Handbook.pdf](https://drive.google.com/file/d/1Handbook"
        in answer.text
    )
    assert [s["title"] for s in answer.sources] == ["Parker O-Ring Handbook.pdf"]
    assert answer.tools_used == {"search_references": 1, "calculate": 1}
    assert answer.ungrounded_links is False
    assert len(records) == 3
    first = client.requests[0]
    assert "Recommendation mode" in first["messages"][0]["content"]
    assert {t["function"]["name"] for t in first["tools"]} == {
        "read_file",
        "search_references",
        "calculate",
    }


def test_a_recommendation_gets_the_longer_time_limit_and_more_rounds(
    monkeypatch, found, kit
):
    run, _, _ = start_recommend(monkeypatch, found, kit, fake.reply("Done."))

    assert run._stream.seconds == llm.RECOMMEND_SECONDS > llm.ANSWER_SECONDS
    assert run._stream.max_rounds == llm.RECOMMEND_ROUNDS > 1


def test_a_normal_answer_has_no_recommendation_prompt_or_tools(monkeypatch, found):
    run, _, client = start(monkeypatch, found, fake.reply("Plain answer."))

    run.run()

    request = client.requests[0]
    assert "Recommendation mode" not in request["messages"][0]["content"]
    assert [t["function"]["name"] for t in request["tools"]] == ["read_file"]
    assert run._stream.seconds == llm.ANSWER_SECONDS


def test_a_failed_search_is_noted_for_the_member(monkeypatch, found, kit):
    def boom(arguments, seconds_left):
        raise RuntimeError("index down")

    kit.search_references = boom
    run, _, _ = start_recommend(
        monkeypatch,
        found,
        kit,
        fake.tool_request(("s1", "search_references", '{"query": "x"}')),
        fake.reply("From the team's sources only."),
    )

    answer = run.run()

    assert answering.NOTE_NO_SEARCH in answering.notes_for(answer)


def test_a_reference_link_the_model_invents_is_still_removed(monkeypatch, found, kit):
    run, _, _ = start_recommend(
        monkeypatch,
        found,
        kit,
        fake.reply(
            "See [Shigley](https://drive.google.com/file/d/1MadeUpMadeUpMadeUp/view)."
        ),
    )

    answer = run.run()

    assert "MadeUp" not in answer.text and "Shigley" in answer.text
    assert answer.ungrounded_links is True
