"""The task page and coach chat, end to end, with a fake model and fake Drive."""

import json
import logging
from datetime import timedelta

import fake_openai as fake
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import auth
import drive
import llm
import main
from models import CoachMessage, LlmCall, Task, utc_now

LINK = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"
HTMX = {"HX-Request": "true"}


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    """Anything that would reach the model or Drive fails the test instead."""
    monkeypatch.delenv("COACH_DAILY_LIMIT", raising=False)

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
    return session.exec(select(Task)).one()


def script(monkeypatch, *items):
    model = fake.FakeClient(items)
    monkeypatch.setattr(llm, "get_client", lambda: model)
    return model


def fake_drive(monkeypatch):
    result = {
        "title": "Latch design notes",
        "link": LINK,
        "mime_type": "application/vnd.google-apps.document",
        "modified_time": "2026-09-30T12:00:00Z",
        "folder_path": "Crush / Enclosure",
        "locations": [
            {
                "link": LINK,
                "mime_type": "application/vnd.google-apps.document",
                "modified_time": "2026-09-30T12:00:00Z",
                "folder_path": "Crush / Enclosure",
            }
        ],
    }
    monkeypatch.setattr(drive, "search", lambda query, limit=10: [result])
    monkeypatch.setattr(drive, "read_text", lambda file_id: "Use the two-point latch.")


def ask(client, text="How should the latch work?", **kwargs):
    return client.post(
        "/tasks/M04/coach", data={"message": text}, headers=HTMX, **kwargs
    )


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
    assert '<label for="coach-message">' in html
    assert 'aria-live="polite"' in html
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

    assert "<h2 id=\"details-heading\">Details</h2>" in page
    for label in ("Description", "Dependencies", "Exploratory questions", "Skills learned",
                  "Requirements", "People to ask", "Documentation", "Due date"):
        assert f"<dt>{label}</dt>" in page
    assert "Cut two panels.\nDeburr the edges." in page
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

    assert "<dt>Skills learned</dt>" in page
    assert "<dt>Description</dt>" not in page
    assert "<dt>Due date</dt>" not in page


def test_task_page_has_no_details_section_without_tracker_text(task, client):
    assert "details-heading" not in client.get("/tasks/M04").text


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


def test_coach_context_skips_empty_fields_and_shortens_long_ones(task, session):
    import coach

    assert list(coach.task_context(task)) == ["Code", "Title", "Subsystem", "Type", "Stage"]

    task.description = "word " * 1000
    context = coach.task_context(task)

    assert len(context["Description"]) < coach.MAX_CONTEXT_FIELD_CHARS + 30
    assert context["Description"].endswith("(shortened)")


def test_unknown_task_is_a_plain_404(client, session):
    response = client.get("/tasks/ZZ99")

    assert response.status_code == 404
    assert "Task not found" in response.text
    assert '<nav aria-label="Main">' in response.text


def test_task_page_survives_a_database_failure(monkeypatch, task, client):
    import coach

    def boom(session, task_id):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(coach, "_history", boom)

    response = client.get("/tasks/M04")

    assert response.status_code == 503
    assert "secret" not in response.text
    assert '<nav aria-label="Main">' in response.text


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

    assert "Sam Example" in html  # in the header and "Asking as"
    assert "Log out" in html


# --- the answer ---


def test_answer_flow_cites_sources_and_logs_every_call(
    monkeypatch, session, task, client
):
    fake_drive(monkeypatch)
    script(
        monkeypatch,
        fake.tool_request(("c1", "drive_search", json.dumps({"query": "latch"}))),
        fake.tool_request(("c2", "drive_read_text", json.dumps({"link": LINK}))),
        fake.reply(f"Which latch style fits? The notes say two-point: {LINK}."),
    )

    response = ask(client)

    assert response.status_code == 200
    html = response.text
    assert "You" in html and "How should the latch work?" in html
    assert "Which latch style fits?" in html
    assert f'<a href="{LINK}"' in html  # the cited URL is clickable
    assert "Latch design notes" in html and "modified 2026-09-30" in html  # sources
    assert response.headers["HX-Trigger"] == "coachSent"

    calls = stored(session, LlmCall)
    assert len(calls) == 3
    assert {c.status for c in calls} == {"ok"}
    assert {(c.task_id, c.member, c.model) for c in calls} == {
        (task.id, "sam@example.edu", "claude-haiku-4-5")
    }
    assert [c.input_tokens for c in calls] == [80, 80, 100]
    assert [c.output_tokens for c in calls] == [15, 15, 20]
    assert calls[2].cost == pytest.approx(0.0012)
    assert all(c.latency_ms >= 0 for c in calls)


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
    sent = [(m["role"], m["content"]) for m in model.requests[1]["messages"][1:]]
    assert sent == [
        ("user", "First question"),
        ("assistant", "First answer."),
        ("user", "Second question"),
    ]


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


def test_unsourced_machining_numbers_get_a_warning(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("Run the endmill at 12000 rpm."))

    html = ask(client).text

    assert "numbers without a source" in html
    saved = next(m for m in stored(session, CoachMessage) if m.role == "assistant")
    assert "numbers without a source" in json.loads(saved.meta)["notes"][0]
    assert "numbers without a source" in client.get("/tasks/M04").text  # persists


# --- failures: plain message, page still works ---


@pytest.mark.parametrize(
    ("failure", "status", "message"),
    [
        (fake.budget_exceeded, "budget_exceeded", "AI budget is used up"),
        (fake.rate_limited, "rate_limited", "rate limit"),
        (fake.timed_out, "timeout", "took too long"),
        (fake.server_error, "error", "failed to answer"),
    ],
)
def test_model_failures_say_so_and_the_page_keeps_working(
    monkeypatch, session, task, client, caplog, failure, status, message
):
    script(monkeypatch, failure())

    with caplog.at_level(logging.DEBUG):
        response = ask(client)

    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert message in response.text
    assert "sk-secret-123" not in response.text + caplog.text
    assert "sk-test-not-a-real-key" not in response.text + caplog.text
    assert [c.status for c in stored(session, LlmCall)] == [status]
    assert [m.role for m in stored(session, CoachMessage)] == [
        "user"
    ]  # no answer saved
    # The rest of the page, and the app, still work.
    page = client.get("/tasks/M04")
    assert page.status_code == 200 and "Finish enclosure wall panels" in page.text
    assert client.get("/health").status_code == 200


def test_drive_failure_still_answers_and_names_drive(
    monkeypatch, session, task, client
):
    def broken(query, limit=10):
        raise drive.DriveError("Google Drive search failed. Check network access.")

    monkeypatch.setattr(drive, "search", broken)
    script(
        monkeypatch,
        fake.tool_request(("c1", "drive_search", json.dumps({"query": "latch"}))),
        fake.reply("Drive could not be reached, so I can't cite the team's notes."),
    )

    response = ask(client)

    assert response.status_code == 200
    assert "Google Drive failed" in response.text
    assert "Drive could not be reached" in response.text
    assert "Google Drive failed" in client.get("/tasks/M04").text  # saved note


def test_missing_model_key_says_so(monkeypatch, task, client):
    monkeypatch.undo()  # drop the autouse refusals but keep going without a key
    monkeypatch.delenv("GLASSROOM_API_KEY")

    response = ask(client)

    assert "isn&#39;t set up" in response.text


def test_failed_save_does_not_lose_the_answer(monkeypatch, task, client):
    import coach

    script(monkeypatch, fake.reply("An answer."))

    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(coach, "LlmCall", boom)

    response = ask(client)

    assert "An answer." in response.text
    assert "could not be saved" in response.text


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

    assert "took too long" in first.text
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


def test_plain_form_failure_shows_the_whole_page_with_the_error(
    monkeypatch, task, client
):
    script(monkeypatch, fake.budget_exceeded())

    response = client.post("/tasks/M04/coach", data={"message": "Hello"})

    assert response.status_code == 200
    assert "AI budget is used up" in response.text
    assert "Finish enclosure wall panels" in response.text
    assert ">Hello</textarea>" in response.text  # the message isn't lost


def test_chat_is_phone_friendly_and_accessible(task, client):
    html = client.get("/tasks/M04").text

    assert 'name="viewport" content="width=device-width, initial-scale=1"' in html
    assert html.count("<h1>") == 1
    assert 'hx-post="/tasks/M04/coach"' in html
    assert 'role="log"' in html
    assert 'role="status"' in html  # the "Thinking…" indicator
