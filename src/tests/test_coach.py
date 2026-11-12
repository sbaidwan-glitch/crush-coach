"""The task page and coach chat, end to end, with a fake model and a made-up index."""

import json
import logging
import re
from datetime import timedelta
from pathlib import Path

import fake_openai as fake
import pytest
from fastapi.testclient import TestClient
from index_helpers import add_file
from sqlmodel import select

import auth
import coach
import decisions
import drive
import llm
import main
from models import (
    CoachMessage,
    Decision,
    LlmCall,
    Task,
    TeamFact,
    utc_now,
)

STATIC = Path(__file__).resolve().parents[1] / "static"
LINK = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"
DOC_ID = "1AbCdEfGhIjKlMnOp"
HTMX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    """Anything that would reach the model or Drive fails the test instead."""
    monkeypatch.delenv("COACH_DAILY_LIMIT", raising=False)
    monkeypatch.delenv("COACH_MODEL", raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("tried to use a real service")

    monkeypatch.setattr(llm, "get_client", refuse)
    monkeypatch.setattr(drive, "search", refuse)
    monkeypatch.setattr(drive, "read_text", refuse)


SAM = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}
ALEX = {"name": "Alex Example", "email": "alex@example.edu", "role": "member"}


def sign_in_as(user):
    main.app.dependency_overrides[auth.require_user] = lambda: user


@pytest.fixture
def client():
    """A signed-in member (Sam); sign_in_as() switches who is signed in."""
    sign_in_as(SAM)
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def task(session):
    """The task M04, and an index with one Google Doc about the latch in it."""
    session.add(
        Task(
            code="M04",
            title="Finish enclosure wall panels",
            subsystem="Enclosure",
            task_type="Fabricate",
            stage="drawing",
        )
    )
    session.commit()
    add_file(
        session,
        "Latch design notes",
        folder="Crush / Enclosure",
        modified="2026-09-30",
        chunks=["Use the two-point latch on the lid. The latch holds the lid shut."],
        drive_id=DOC_ID,
        link=LINK,
    )
    return session.exec(select(Task)).one()


def script(monkeypatch, *items):
    model = fake.FakeClient(items)
    monkeypatch.setattr(llm, "get_client", lambda: model)
    return model


def ask(client, text="How should the latch work?", **kwargs):
    return client.post(
        "/tasks/M04/coach", data={"message": text}, headers=HTMX, **kwargs
    )


def stream(client, text="How should the latch work?", **data):
    """POST to the streaming route; returns the list of events it sent."""
    response = client.post("/tasks/M04/coach/stream", data={"message": text, **data})
    assert response.status_code == 200
    return [json.loads(line) for line in response.text.splitlines() if line]


def stored(session, model):
    session.expire_all()
    return list(session.exec(select(model)).all())


# --- the page ---


def test_task_page_shows_the_task_and_never_calls_the_model(task, client):
    response = client.get("/tasks/M04")

    assert response.status_code == 200
    html = response.text
    assert "M04" in html and "Finish enclosure wall panels" in html
    assert "Enclosure" in html and "Fabricate" in html
    assert 'for="coach-message"' in html
    assert 'aria-live="polite"' in html
    assert "Progress" in html and "About this task" in html and "History" in html
    # (a real model/Drive call would have raised in the autouse fixture)


def test_board_links_each_task_to_its_page(task, client):
    html = client.get("/tasks").text

    assert 'href="/tasks/M04"' in html


def fill_details(session, task):
    task.description = "Cut two panels.\nDeburr the edges."
    task.dependencies = "M01 first"
    task.exploratory_questions = "Which stock?"
    task.skills = "Waterjet basics"
    task.requirements = "Flat to spec"
    task.people_to_ask = "Riley Sample"
    task.documentation = f"See {LINK} and <b>notes</b>"
    task.due_date = "Week 3"
    session.add(task)
    session.commit()


def test_task_page_shows_the_tracker_details(task, client, session):
    fill_details(session, task)

    page = client.get("/tasks/M04").text

    assert 'id="about-heading">About this task</h2>' in page
    for label in (
        "Dependencies",
        "Exploratory questions",
        "Skills learned",
        "Requirements",
        "People to ask",
        "Documentation",
        "Due date",
    ):
        assert f'<div class="field-label">{label}</div>' in page
    assert "Cut two panels.\nDeburr the edges." in page  # the description leads
    assert "Riley Sample" in page and "Week 3" in page
    # Google links are clickable; any other markup in a cell is escaped.
    assert f'<a href="{LINK}"' in page
    assert "&lt;b&gt;notes&lt;/b&gt;" in page
    assert "<b>notes</b>" not in page


def test_task_page_omits_details_that_are_empty(task, client, session):
    task.skills = "Waterjet basics"
    session.add(task)
    session.commit()

    page = client.get("/tasks/M04").text

    assert '<div class="field-label">Skills learned</div>' in page
    assert '<div class="field-label">Due date</div>' not in page
    assert '<div class="field-label">Dependencies</div>' not in page


def test_task_page_says_so_when_the_tracker_has_no_description(task, client):
    page = client.get("/tasks/M04").text

    assert "The tracker has no description for this task." in page
    assert '<div class="field-label">Requirements</div>' not in page


def test_coach_context_skips_empty_fields_and_shortens_long_ones(task, session):
    import coach

    assert list(coach.task_context(task)) == [
        "Code",
        "Title",
        "Subsystem",
        "Type",
        "Stage",
    ]

    task.description = "word " * 1000
    context = coach.task_context(task)

    assert len(context["Description"]) < coach.MAX_CONTEXT_FIELD_CHARS + 30
    assert context["Description"].endswith("(shortened)")


def test_unknown_task_is_a_plain_404(client, session):
    response = client.get("/tasks/ZZ99")

    assert response.status_code == 404
    assert "Task not found" in response.text
    assert '<nav class="sidebar" aria-label="Main">' in response.text


def test_task_page_survives_a_database_failure(monkeypatch, task, client):
    import coach

    def boom(session, task_id):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(coach, "_history", boom)

    response = client.get("/tasks/M04")

    assert response.status_code == 503
    assert "secret" not in response.text
    assert '<nav class="sidebar" aria-label="Main">' in response.text


# --- access: signed-in members only ---


def test_coach_requires_sign_in(monkeypatch, task):
    main.app.dependency_overrides.clear()  # no override: the real sign-in applies
    script(monkeypatch)  # any model call would raise
    with TestClient(main.app, follow_redirects=False) as anonymous:
        page = anonymous.get("/tasks/M04")
        post = anonymous.post("/tasks/M04/coach", data={"message": "hi"})

    for response in (page, post):
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
    assert "Finish enclosure wall panels" not in page.text


def test_task_page_shows_who_is_signed_in(task, client):
    html = client.get("/tasks/M04").text

    assert "Sam Example" in html  # signed in as, and the label of the message box
    assert 'href="/logout">Sign out</a>' in html


# --- what the model is given ---


def test_one_streamed_model_call_gets_facts_task_decisions_and_passages(
    monkeypatch, session, task, client
):
    session.add(TeamFact(text="Current season is 2026-27"))
    decision = Decision(
        subsystem="Enclosure",
        topic="Latch style",
        decision="Use the two-point cam latch.",
        rationale="It seals evenly.",
        status="confirmed",
        proposed_by="sam@example.edu",
        confirmed_at=utc_now(),
    )
    proposal = Decision(
        subsystem="Enclosure",
        topic="Toggle proposal",
        decision="Use a toggle.",
        rationale="Cheap.",
        status="proposed",
        proposed_by="sam@example.edu",
    )
    session.add_all([decision, proposal])
    add_file(session, "Enclosure_v2.SLDPRT", kind="cad", modified="2026-10-09")
    session.commit()
    model = script(monkeypatch, fake.reply("Use the cam latch."))

    ask(client, "Which latch for the enclosure?")

    assert len(model.requests) == 1  # one call; no search loop
    request = model.requests[0]
    assert request["stream"] is True and request["model"] == "claude-sonnet-5"
    system = request["messages"][0]["content"]
    assert "- Current season is 2026-27" in system
    assert "- Code: M04" in system and "- Subsystem: Enclosure" in system
    sent = request["messages"][-1]["content"]
    assert "Use the two-point cam latch." in sent  # the confirmed decision
    assert "Toggle proposal" not in sent  # not binding
    assert "Use the two-point latch on the lid." in sent  # the Drive passage
    assert f"link: {LINK}" in sent and "folder: Crush / Enclosure" in sent
    assert "Enclosure_v2.SLDPRT" in sent  # the newest matching CAD file
    assert sent.rstrip().endswith("Which latch for the enclosure?")


def test_coach_is_told_the_tracker_details(monkeypatch, task, client, session):
    fill_details(session, task)
    model = script(monkeypatch, fake.reply("Ask Riley Sample first."))

    response = ask(client)

    assert response.status_code == 200
    system = model.requests[0]["messages"][0]["content"]
    assert "- Code: M04" in system
    assert "- Description: Cut two panels. Deburr the edges." in system  # one line
    assert "- Dependencies: M01 first" in system
    assert "- Exploratory questions: Which stock?" in system
    assert "- Skills learned: Waterjet basics" in system
    assert "- Requirements: Flat to spec" in system
    assert "- People to ask: Riley Sample" in system
    assert "- Due date: Week 3" in system
    assert LINK in system


def test_the_prompt_is_the_rewritten_system_prompt(monkeypatch, task, client):
    model = script(monkeypatch, fake.reply("ok"))

    ask(client)

    system = model.requests[0]["messages"][0]["content"]
    assert system.startswith(llm.system_prompt())
    assert "Never ask which season or which vehicle" in system


# --- the answer ---


def test_answer_cites_sources_and_logs_the_call(monkeypatch, session, task, client):
    script(
        monkeypatch,
        fake.reply(f"Use the two-point latch. Sources: [Latch design notes]({LINK})."),
    )

    response = ask(client)

    assert response.status_code == 200
    html = response.text
    # The page already shows the member's message (coach.js adds it on submit).
    assert "How should the latch work?" not in html
    assert "Use the two-point latch." in html
    assert f'<a href="{LINK}"' in html  # the cited URL is clickable
    assert "Latch design notes" in html and "Modified 2026-09-30" in html  # sources
    assert response.headers["HX-Trigger"] == "coachSent"

    [call] = stored(session, LlmCall)
    assert (call.task_id, call.member, call.model, call.status) == (
        task.id,
        "sam@example.edu",
        "claude-sonnet-5",
        "ok",
    )
    assert (call.input_tokens, call.output_tokens) == (100, 20)
    assert call.cost == pytest.approx(0.0012)
    assert call.latency_ms >= 0


def test_the_saved_answer_remembers_where_each_source_is_in_the_index(
    monkeypatch, session, task, client
):
    script(monkeypatch, fake.reply(f"See {LINK}."))

    ask(client)

    saved = next(m for m in stored(session, CoachMessage) if m.role == "assistant")
    [source] = json.loads(saved.meta)["sources"]
    assert source["title"] == "Latch design notes" and source["drive_id"] == DOC_ID
    assert source["chunk_ids"]


def test_conversation_is_saved_and_shown_on_reload(monkeypatch, session, task, client):
    model = script(
        monkeypatch, fake.reply("First answer."), fake.reply("Second answer.")
    )

    ask(client, "First question")
    ask(client, "Second question")

    rows = stored(session, CoachMessage)
    assert [(r.role, r.content) for r in sorted(rows, key=lambda r: r.id)] == [
        ("user", "First question"),
        ("assistant", "First answer."),
        ("user", "Second question"),
        ("assistant", "Second answer."),
    ]
    html = client.get("/tasks/M04").text
    order = ["First question", "First answer.", "Second question", "Second answer."]
    assert [html.index(text) for text in order] == sorted(html.index(t) for t in order)
    # The second request carried the first exchange as context.
    sent = [(m["role"], m["content"]) for m in model.requests[1]["messages"][1:-1]]
    assert sent == [("user", "First question"), ("assistant", "First answer.")]
    assert (
        model.requests[1]["messages"][-1]["content"]
        .rstrip()
        .endswith("Second question")
    )


def test_a_follow_up_keeps_the_sources_from_earlier_in_the_thread(
    monkeypatch, session, task, client
):
    model = script(
        monkeypatch,
        fake.reply(f"The two-point latch ({LINK})."),
        fake.reply(f"It is the same document: {LINK}."),
    )
    ask(client, "How should the latch work?")

    # Nothing in the follow-up matches the document (and it is too long to borrow the
    # last question's words), so only the thread can bring the source back.
    second = ask(client, "zxqv wkrp yzbl ghtn mpdq vbnx fjkl crtw")

    sent = model.requests[1]["messages"][-1]["content"]
    assert "Sources cited earlier in this conversation" in sent
    assert "Use the two-point latch on the lid." in sent
    assert f'<a href="{LINK}"' in second.text  # it may be cited again
    assert "Latch design notes" in second.text  # and is listed as a source again


def test_history_is_per_task(monkeypatch, session, task, client):
    session.add(
        Task(
            code="M05",
            title="Other",
            subsystem="Gripper",
            task_type="Design",
            stage="requirements",
        )
    )
    session.commit()
    script(monkeypatch, fake.reply("Answer for M04."))
    ask(client, "About M04")

    assert "About M04" in client.get("/tasks/M04").text
    assert "About M04" not in client.get("/tasks/M05").text


def test_model_output_is_escaped_and_only_google_links_are_clickable(
    monkeypatch, task, client
):
    script(
        monkeypatch,
        fake.reply(
            "<script>alert(1)</script> see https://evil.example/x and " + LINK + "."
        ),
    )

    html = ask(client).text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="https://evil.example' not in html
    assert f'<a href="{LINK}"' in html
    assert 'rel="noopener noreferrer"' in html


def test_unsourced_machining_numbers_get_a_warning(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("Run the endmill at 12000 rpm."))

    html = ask(client).text

    assert "numbers without a source" in html
    saved = next(m for m in stored(session, CoachMessage) if m.role == "assistant")
    assert "numbers without a source" in json.loads(saved.meta)["notes"][0]
    assert "numbers without a source" in client.get("/tasks/M04").text  # persists


def test_an_invented_link_is_removed_and_the_member_is_told(
    monkeypatch, session, task, client
):
    invented = "https://docs.google.com/document/d/1MadeUpDocIdThatNoSourceHad/edit"
    script(monkeypatch, fake.reply(f"It is in the CAD folder: {invented}"))

    html = ask(client).text

    assert invented not in html
    assert "was not one of the sources" in html
    saved = stored(session, CoachMessage)[-1]
    assert invented not in saved.content


def test_a_decision_database_failure_is_noted_and_the_answer_still_arrives(
    monkeypatch, session, task, client
):
    def broken(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(decisions, "search_confirmed", broken)
    script(monkeypatch, fake.reply("I couldn't check the decisions, so ask your lead."))

    response = ask(client)

    assert response.status_code == 200
    assert "ask your lead" in response.text
    assert "decision records couldn&#39;t be checked" in response.text
    assert "secret" not in response.text
    assert len(stored(session, LlmCall)) == 1  # the exchange was still logged


# --- the one live read ---


def test_the_model_may_read_one_listed_doc_live(monkeypatch, session, task, client):
    reads = []
    monkeypatch.setattr(
        drive, "read_text", lambda file_id: reads.append(file_id) or "Full doc text."
    )
    model = script(
        monkeypatch,
        fake.tool_request(("r1", "read_file", json.dumps({"file_id": DOC_ID}))),
        fake.reply(f"The full doc says so ({LINK})."),
    )

    html = ask(client).text

    assert reads == [DOC_ID]
    assert "The full doc says so" in html
    assert "Full doc text." in model.requests[1]["messages"][-1]["content"]
    assert [c.status for c in stored(session, LlmCall)] == ["ok", "ok"]  # both logged


def test_a_file_that_was_not_shown_cannot_be_read(monkeypatch, session, task, client):
    model = script(
        monkeypatch,
        fake.tool_request(
            ("r1", "read_file", json.dumps({"file_id": "somethingElse123"}))
        ),
        fake.reply("I could not open it; the passages above don't say."),
    )

    html = ask(client).text  # drive.read_text would raise if it were called

    assert "Reading a Google Doc live failed" in html
    assert "could not be read" in model.requests[1]["messages"][-1]["content"]


def test_a_slow_live_read_ends_at_the_time_limit(monkeypatch, session, task, client):
    import threading

    release = threading.Event()
    monkeypatch.setattr(drive, "read_text", lambda file_id: release.wait(5))
    monkeypatch.setattr(llm, "ANSWER_SECONDS", 0.3)
    script(
        monkeypatch,
        fake.tool_request(("r1", "read_file", json.dumps({"file_id": DOC_ID}))),
        fake.reply("From the passages alone."),
    )

    try:
        response = ask(client)
    finally:
        release.set()

    assert (
        "Reading a Google Doc live failed" in response.text
        or "Took too long" in response.text
    )


# --- failures: stays on screen, says what failed, can be retried ---


@pytest.mark.parametrize(
    ("failure", "status", "message"),
    [
        (fake.budget_exceeded, "budget_exceeded", "AI budget is used up"),
        (fake.rate_limited, "rate_limited", "rate limit"),
        (fake.timed_out, "timeout", "Took too long"),
        (fake.unreachable, "timeout", "Couldn&#39;t reach the AI service"),
        (fake.server_error, "error", "failed to answer"),
    ],
)
def test_model_failures_say_what_failed_and_offer_retry(
    monkeypatch, session, task, client, caplog, failure, status, message
):
    script(monkeypatch, failure())

    with caplog.at_level(logging.DEBUG):
        response = ask(client)

    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert message in response.text
    assert 'name="retry"' in response.text and ">Retry</button>" in response.text
    assert "sk-secret-123" not in response.text + caplog.text
    assert "sk-test-not-a-real-key" not in response.text + caplog.text
    assert [c.status for c in stored(session, LlmCall)] == [status]
    roles = [m.role for m in sorted(stored(session, CoachMessage), key=lambda m: m.id)]
    assert roles == ["user", "error"]  # the question is kept; no answer saved
    # The rest of the page, and the app, still work.
    page = client.get("/tasks/M04")
    assert page.status_code == 200 and "Finish enclosure wall panels" in page.text
    assert client.get("/health").status_code == 200


def test_a_failed_message_is_still_on_the_page_after_a_reload_with_retry(
    monkeypatch, task, client
):
    script(monkeypatch, fake.timed_out())
    ask(client, "Which latch for the lid?")

    html = client.get("/tasks/M04").text

    assert "Which latch for the lid?" in html  # never lost
    assert "Took too long" in html
    assert re.search(
        r'<form[^>]*data-coach-retry[^>]*action="/tasks/M04/coach"|'
        r'<form[^>]*action="/tasks/M04/coach"[^>]*data-coach-retry',
        html,
    )
    assert html.index("Which latch for the lid?") < html.index("Took too long")


def test_retry_answers_the_same_message_without_a_second_copy(
    monkeypatch, session, task, client
):
    script(monkeypatch, fake.timed_out(), fake.reply("The two-point latch."))
    ask(client, "Which latch for the lid?")
    failed = next(m for m in stored(session, CoachMessage) if m.role == "user")

    events = stream(client, "", retry=str(failed.id))

    assert events[-1]["type"] == "done" and "two-point latch" in events[-1]["html"]
    rows = sorted(stored(session, CoachMessage), key=lambda m: m.id)
    assert [(r.role, r.content) for r in rows] == [
        ("user", "Which latch for the lid?"),
        ("assistant", "The two-point latch."),
    ]  # the failure went away; the question appears once
    html = client.get("/tasks/M04").text
    assert "Retry" not in html


def test_a_retry_that_fails_again_shows_a_fresh_failure(
    monkeypatch, session, task, client
):
    script(monkeypatch, fake.timed_out(), fake.rate_limited())
    ask(client, "Which latch for the lid?")
    failed = next(m for m in stored(session, CoachMessage) if m.role == "user")

    events = stream(client, "", retry=str(failed.id))

    assert events[-1]["type"] == "failed"
    assert "rate limit" in events[-1]["html"] and 'name="retry"' in events[-1]["html"]
    assert [m.role for m in stored(session, CoachMessage)].count("error") == 1


def test_retry_is_only_offered_on_the_latest_failure(
    monkeypatch, session, task, client
):
    script(monkeypatch, fake.timed_out(), fake.reply("Answer."))
    ask(client, "First")
    ask(client, "Second")

    html = client.get("/tasks/M04").text

    assert "Took too long" in html and 'name="retry"' not in html


def test_retry_does_not_use_up_the_daily_cap(monkeypatch, session, task, client):
    monkeypatch.setenv("COACH_DAILY_LIMIT", "1")
    script(monkeypatch, fake.timed_out(), fake.reply("Answer."))
    ask(client, "Which latch?")
    failed = next(m for m in stored(session, CoachMessage) if m.role == "user")

    events = stream(client, "", retry=str(failed.id))

    assert events[-1]["type"] == "done"


@pytest.mark.parametrize("who", ["other member", "unknown id", "already answered"])
def test_a_retry_that_does_not_apply_is_rejected_and_calls_no_model(
    monkeypatch, session, task, client, who
):
    script(monkeypatch, fake.timed_out())
    ask(client, "Which latch?")
    row = next(m for m in stored(session, CoachMessage) if m.role == "user")
    retry_id = 9999
    if who == "other member":
        sign_in_as(ALEX)
        retry_id = row.id
    elif who == "already answered":
        session.add(
            CoachMessage(
                task_id=task.id, member="Coach", role="assistant", content="Hi"
            )
        )
        session.commit()
        retry_id = row.id

    events = stream(client, "", retry=str(retry_id))  # a second model call would raise

    assert [e["type"] for e in events] == ["rejected"]


def test_a_failed_message_is_not_shown_to_the_model_as_a_turn(
    monkeypatch, session, task, client
):
    model = script(monkeypatch, fake.timed_out(), fake.reply("Answer."))
    ask(client, "Never answered question")

    ask(client, "A new question")

    roles = [m["role"] for m in model.requests[1]["messages"]]
    assert roles == ["system", "user"]  # only the new question


def test_empty_index_says_so_without_calling_the_model(
    monkeypatch, session, task, client
):
    from sqlmodel import delete

    from models import DriveChunk, DriveFile

    session.exec(delete(DriveChunk))
    session.exec(delete(DriveFile))
    session.commit()
    script(monkeypatch)  # any model call would raise

    response = ask(client)

    assert "Drive index is empty: ask a lead to sync." in response.text
    assert ">Retry</button>" in response.text
    assert stored(session, LlmCall) == []
    assert [m.role for m in stored(session, CoachMessage)] == ["user", "error"]


def test_missing_model_key_says_so(monkeypatch, task, client):
    monkeypatch.undo()  # drop the autouse refusals but keep going without a key
    monkeypatch.delenv("GLASSROOM_API_KEY")

    response = ask(client)

    assert "isn&#39;t set up" in response.text


def test_failed_save_does_not_lose_the_answer(monkeypatch, task, client):
    script(monkeypatch, fake.reply("An answer."))

    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(coach, "LlmCall", boom)

    response = ask(client)

    assert "An answer." in response.text
    assert "could not be saved" in response.text


def test_a_database_failure_before_the_message_is_saved_keeps_the_text_in_the_box(
    monkeypatch, task, client
):
    def boom(session, member):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(coach, "messages_today", boom)

    events = stream(client)

    assert [e["type"] for e in events] == ["rejected"]
    assert "Couldn't reach the database" in events[0]["message"]
    assert "secret" not in json.dumps(events)


def test_a_dropped_connection_keeps_the_message_and_logs_the_call(
    monkeypatch, session, task
):
    slow = fake.reply("one two three four five six seven eight", delay=0.01)
    script(monkeypatch, slow)
    run = coach.converse("M04", SAM, "Which latch for the lid?")

    next(run)  # the first step
    while True:
        event = next(run)
        if event["type"] == "text":
            break
    run.close()  # the page went away mid-answer

    rows = sorted(stored(session, CoachMessage), key=lambda m: m.id)
    assert next((r.role, r.content) for r in rows) == (
        "user",
        "Which latch for the lid?",
    )
    assert rows[-1].role == "error" and "connection dropped" in rows[-1].content
    assert len(stored(session, LlmCall)) == 1


# --- the time limit ---


def test_past_the_time_limit_the_answer_so_far_is_kept_with_a_note(
    monkeypatch, session, task, client
):
    monkeypatch.setattr(llm, "ANSWER_SECONDS", 0.1)
    script(
        monkeypatch,
        fake.reply("one two three four five six seven eight nine ten", delay=0.03),
    )

    html = ask(client).text

    assert "stopped early" in html
    assert "ten" not in html.split("stopped early")[0].split()[-1:]
    saved = next(m for m in stored(session, CoachMessage) if m.role == "assistant")
    assert 0 < len(saved.content.split()) < 10


def test_nothing_before_the_time_limit_is_took_too_long(
    monkeypatch, session, task, client
):
    monkeypatch.setattr(llm, "ANSWER_SECONDS", 0.05)
    script(monkeypatch, fake.reply("late late late", delay=0.2))

    html = ask(client).text

    assert "Took too long" in html and ">Retry</button>" in html


# --- the streaming route ---


def test_the_stream_sends_steps_then_text_then_the_finished_answer(
    monkeypatch, task, client
):
    script(monkeypatch, fake.reply("Use the **two-point** latch. Next step: open it."))

    events = stream(client)

    kinds = [e["type"] for e in events]
    assert kinds[0] == "step" and "Searching" in events[0]["text"]
    assert kinds[-1] == "done"
    assert "text" in kinds and kinds.index("text") < kinds.index("done")
    assert all(k in ("step", "text", "done") for k in kinds)
    text_events = [e for e in events if e["type"] == "text"]
    assert "<strong>two-point</strong>" in text_events[-1]["html"]  # rendered, safe
    assert "two-point" in events[-1]["html"] and "msg--coach" in events[-1]["html"]


def test_text_on_the_way_is_already_safe_html(monkeypatch, task, client):
    script(monkeypatch, fake.reply("<script>alert(1)</script> hello"))

    events = stream(client)

    for event in events:
        assert "<script>" not in event.get("html", "")


def test_a_half_written_link_is_not_shown_as_removed(monkeypatch, task, client):
    script(monkeypatch, fake.reply(f"See {LINK} now."))

    events = stream(client)

    shown = "".join(e["html"] for e in events if e["type"] == "text")
    assert "link removed" not in shown


def test_the_stream_rejects_a_blank_message_and_sends_nothing_to_the_model(
    monkeypatch, session, task, client
):
    script(monkeypatch)

    events = stream(client, "   ")

    assert [e["type"] for e in events] == ["rejected"]
    assert "Write a message" in events[0]["message"]
    assert stored(session, CoachMessage) == []


def test_the_stream_for_an_unknown_task_is_a_404(client, session):
    response = client.post("/tasks/ZZ99/coach/stream", data={"message": "hi"})

    assert response.status_code == 404


def test_the_stream_requires_sign_in(monkeypatch, task):
    main.app.dependency_overrides.clear()
    script(monkeypatch)
    with TestClient(main.app, follow_redirects=False) as anonymous:
        response = anonymous.post("/tasks/M04/coach/stream", data={"message": "hi"})

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_the_stream_is_not_buffered_or_cached(monkeypatch, task, client):
    script(monkeypatch, fake.reply("ok"))

    response = client.post("/tasks/M04/coach/stream", data={"message": "hi"})

    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"


def test_a_failure_in_the_stream_is_a_failed_event_with_retry(
    monkeypatch, task, client
):
    script(monkeypatch, fake.timed_out())

    events = stream(client)

    assert events[-1]["type"] == "failed"
    assert "Took too long" in events[-1]["html"]
    assert 'name="retry"' in events[-1]["html"]


# --- validation and the daily cap ---


def test_message_is_required_and_length_limited(monkeypatch, task, client):
    script(monkeypatch)  # any model call would raise

    blank_message = ask(client, "   ")
    too_long = ask(client, "x" * 2001)

    assert "Write a message" in blank_message.text
    assert "Write a message" in too_long.text


def test_daily_cap_blocks_the_next_message_without_calling_the_model(
    monkeypatch, session, task, client
):
    monkeypatch.setenv("COACH_DAILY_LIMIT", "2")
    model = script(monkeypatch, fake.reply("one"), fake.reply("two"))
    ask(client, "q1")
    ask(client, "q2")

    blocked = ask(client, "q3")

    assert "used your 2 coach messages" in blocked.text
    assert 'role="alert"' in blocked.text
    assert len(model.requests) == 2  # the model was not called a third time
    assert [m.content for m in stored(session, CoachMessage) if m.role == "user"] == [
        "q1",
        "q2",
    ]
    assert len(stored(session, LlmCall)) == 2


def test_cap_is_per_member_and_ignores_earlier_days(monkeypatch, session, task, client):
    monkeypatch.setenv("COACH_DAILY_LIMIT", "1")
    session.add(
        CoachMessage(
            task_id=task.id,
            member="sam@example.edu",
            role="user",
            content="yesterday",
            created_at=utc_now() - timedelta(days=1, hours=1),
        )
    )
    session.commit()
    script(monkeypatch, fake.reply("for Sam"), fake.reply("for Alex"))

    sam = ask(client, "today")  # yesterday doesn't count
    sam_again = ask(client, "again")
    sign_in_as(ALEX)
    alex = ask(client, "hi")  # someone else is unaffected

    assert "for Sam" in sam.text
    assert "used your 1 coach messages" in sam_again.text
    assert "for Alex" in alex.text


def test_failed_attempts_count_toward_the_cap(monkeypatch, task, client):
    monkeypatch.setenv("COACH_DAILY_LIMIT", "1")
    script(monkeypatch, fake.timed_out())

    first = ask(client, "q1")
    second = ask(client, "q2")

    assert "Took too long" in first.text
    assert "used your 1 coach messages" in second.text


# --- without JavaScript ---


def test_plain_form_post_redirects_back_to_the_page(monkeypatch, task, client):
    script(monkeypatch, fake.reply("Plain answer."))

    response = client.post(
        "/tasks/M04/coach",
        data={"message": "Hello"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/tasks/M04"
    page = client.get("/tasks/M04")
    assert "Plain answer." in page.text
    assert "Hello" in page.text


def test_plain_form_failure_redirects_to_the_page_that_shows_it_with_retry(
    monkeypatch, task, client
):
    script(monkeypatch, fake.budget_exceeded(), fake.reply("Back again."))

    response = client.post(
        "/tasks/M04/coach", data={"message": "Hello"}, follow_redirects=False
    )
    page = client.get("/tasks/M04")

    assert response.status_code == 303
    assert "AI budget is used up" in page.text
    assert "Hello" in page.text and ">Retry</button>" in page.text
    retry = re.search(r'name="retry" value="(\d+)"', page.text)
    again = client.post(
        "/tasks/M04/coach", data={"retry": retry.group(1)}, follow_redirects=False
    )
    assert again.status_code == 303
    assert "Back again." in client.get("/tasks/M04").text


def test_plain_form_with_a_rejected_message_shows_the_page_and_keeps_the_text(
    monkeypatch, task, client
):
    script(monkeypatch)

    response = client.post("/tasks/M04/coach", data={"message": "x" * 2001})

    assert response.status_code == 200
    assert "Write a message" in response.text
    assert "Finish enclosure wall panels" in response.text


def test_chat_is_phone_friendly_and_accessible(task, client):
    html = client.get("/tasks/M04").text

    assert 'name="viewport" content="width=device-width, initial-scale=1"' in html
    assert html.count("<h1 ") == 1
    assert 'hx-post="/tasks/M04/coach"' in html  # the no-streaming fallback
    assert 'role="log"' in html
    assert 'role="status"' in html  # the coach's working slot (see below)


def test_the_members_message_is_a_filled_box_and_the_coachs_has_a_blue_rule(
    session, task, client
):
    session.add(
        CoachMessage(task_id=task.id, member="sam@example.edu", role="user", content="Hi?")
    )
    session.add(
        CoachMessage(task_id=task.id, member="Coach", role="assistant", content="Hello.")
    )
    session.commit()

    html = client.get("/tasks/M04").text

    assert '<article class="msg msg--member">' in html
    assert '<article class="msg msg--coach">' in html


def test_next_step_and_conflicts_get_their_own_boxes(session, task, client):
    session.add(
        CoachMessage(
            task_id=task.id,
            member="Coach",
            role="assistant",
            content=(
                "The sources disagree.\n\n"
                "**Likely current: 0.050 in** (Crush Documentation, Mar 2026). "
                "Older: 0.030 in. A lead should confirm.\n\n"
                "Next step: ask Lee to confirm the clearance."
            ),
            meta=json.dumps(
                {
                    "sources": [
                        {"title": "Crush Documentation", "link": LINK, "modified": "2026-03-30"},
                        {"title": "D1 latch", "link": "/decisions/1", "modified": "2026-10-01"},
                    ],
                    "notes": [],
                }
            ),
        )
    )
    session.commit()

    html = client.get("/tasks/M04").text

    assert '<div class="conflict">' in html and "Likely current: 0.050 in" in html
    assert 'href="/decisions/new?task=M04">Propose as a decision</a>' in html
    assert '<div class="next msg-md"><b>Next step:</b>' in html
    assert "ask Lee to confirm the clearance." in html
    assert '<ul class="sources" aria-label="Sources">' in html
    assert "Modified 2026-03-30" in html and "Decision · confirmed 2026-10-01" in html


def test_a_failed_answer_uses_the_error_box_and_keeps_the_question_above_it(
    monkeypatch, task, client
):
    script(monkeypatch, fake.timed_out())
    ask(client, "Which latch for the lid?")

    html = client.get("/tasks/M04").text

    assert '<article class="msg msg--coach msg--error" role="alert" data-failed>' in html
    assert "Couldn’t get an answer" in html and "Took too long" in html
    assert "Your question is kept above." in html
    assert html.index("Which latch for the lid?") < html.index("Took too long")
    assert '<button class="btn btn--small" type="submit">Retry</button>' in html


def test_the_page_shows_the_scanning_bar_text_the_stream_updates(task, client):
    html = client.get("/tasks/M04").text

    assert 'id="coach-pending" class="thinking"' in html
    assert "Searching the Drive index…" in (STATIC / "coach.js").read_text()


# --- loading state ---


def test_the_page_has_a_live_working_slot_and_a_form_that_disables_while_waiting(
    task, client
):
    html = client.get("/tasks/M04").text

    # The coach's slot is last in the log, hidden at rest, with a polite live region
    # that is always in the page (so screen readers announce each change of text).
    assert re.search(r'<div id="coach-pending"[^>]*\bhidden\b', html)
    assert 'aria-busy="true"' in html
    assert re.search(
        r'<p id="coach-status"[^>]*role="status"[^>]*aria-live="polite"', html
    )
    assert html.index('id="coach-log"') < html.index('id="coach-pending"')
    assert html.index('id="coach-pending"') < html.index('id="coach-form"')
    assert '<fieldset class="ask__fields">' in html
    # coach.js builds the member's message from this template (textContent, no HTML).
    assert '<template id="coach-user-template">' in html


def test_coach_js_streams_shows_the_message_at_once_and_restores_the_form():
    js = (STATIC / "coach.js").read_text()

    assert "/stream" in js and "getReader" in js
    assert "textContent = text" in js  # the member's text is never parsed as HTML
    assert "data-coach-retry" in js
    assert 'aria-busy", "true"' in js  # partial answers are not read out piece by piece
    # The only innerHTML is the server's already-sanitized answer.
    assert (
        js.count("innerHTML") == 2
    )  # the answer so far, and clearing it and "event.html" in js
    # Whatever happens, the form comes back.
    assert "setBusy(false)" in js
    assert "htmx:beforeRequest" in js  # the fallback path is still there


def test_htmx_reply_is_just_the_answer_the_page_already_shows_the_question(
    monkeypatch, task, client
):
    script(monkeypatch, fake.reply("Try the **two-point** latch."))

    html = ask(client, "How should the latch work?").text

    assert "<strong>two-point</strong>" in html
    assert "How should the latch work?" not in html


def test_the_message_is_saved_even_though_the_reply_does_not_repeat_it(
    monkeypatch, session, task, client
):
    script(monkeypatch, fake.reply("Hi."))

    ask(client, "How should the latch work?")

    assert [m.content for m in stored(session, CoachMessage)] == [
        "How should the latch work?",
        "Hi.",
    ]


def test_motion_only_runs_when_the_member_has_not_asked_for_less():
    css = (STATIC / "styles" / "app.css").read_text()
    reduced = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{[^{}]*\{[^{}]*animation: none[^{}]*\}\s*\}",
        css,
    )

    assert reduced, "the scanning bar must stop when reduced motion is asked for"
    assert "transition" not in css


def test_the_old_progress_poll_is_gone(client, task):
    assert client.get("/tasks/M04/coach/progress").status_code in (404, 405)


# --- Markdown in the thread ---


def test_markdown_answers_render_as_lists_and_code_in_the_thread(
    monkeypatch, task, client
):
    script(monkeypatch, fake.reply("Check:\n\n- the **o-ring** groove\n- `M3` screws"))

    ask(client)
    html = client.get("/tasks/M04").text  # on reload, from the saved text

    assert "<li>the <strong>o-ring</strong> groove</li>" in html
    assert "<code>M3</code>" in html
    assert "- the **o-ring**" not in html


def test_member_messages_stay_plain_text(monkeypatch, task, client):
    script(monkeypatch, fake.reply("ok"))

    ask(client, "is **this** <b>bold</b>?")
    page = client.get("/tasks/M04").text

    assert "is **this** &lt;b&gt;bold&lt;/b&gt;?" in page
    assert "<strong>this</strong>" not in page
