"""The work log: the side panel and its two tabs, Tidy my note (one model call, on the
button), posting without a lead, the Crush document write and its retry, the Home banner,
and where notes show up (History, Record). Made-up people and tasks; the model and Google
are fakes."""

import json
import re
from datetime import timedelta

import fake_openai as fake
import pytest
from fake_crush_doc import DOC_ID, FakeCrushDoc
from fake_google import http_error
from fastapi.testclient import TestClient
from sqlmodel import select

import auth
import drive_write
import llm
import localtime
import main
import worklog
from models import (
    CoachMessage,
    Decision,
    DriveChunk,
    DriveFile,
    DriveWrite,
    LlmCall,
    Task,
    TaskEvent,
    WorkNote,
    utc_now,
)

MEMBER = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}
OTHER = {"name": "Riley Sample", "email": "riley@example.edu", "role": "member"}
LEAD = {"name": "Lee Lead", "email": "lee@example.edu", "role": "lead"}
HTMX = {"HX-Request": "true"}
TODAY = localtime.heading(localtime.today())

DID = "tacked the 4 walls and welded 2 corners, one warped a bit so clamped it to the fixture plate"
WHY = "did opposite corners first so the heat pulls evenly"
TIDIED = {
    "did": "Tack-welded all 4 walls and fully welded 2 corners. One warped slightly, so it was clamped to the fixture plate.",
    "why": "Welded opposite corners first so the heat pulls evenly.",
    "question": "Which two corners did you fully weld?",
}


@pytest.fixture
def as_user(monkeypatch):
    clients = []

    def make(user):
        monkeypatch.setattr(auth, "session_user", lambda request: user)
        main.app.dependency_overrides[auth.require_user] = lambda: user
        main.app.dependency_overrides[auth.optional_user] = lambda: user
        c = TestClient(main.app)
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    main.app.dependency_overrides.clear()


@pytest.fixture
def doc(monkeypatch):
    """The Crush document, as a fake with a tab per part of Crush."""
    document = FakeCrushDoc("Enclosure", "Seals", "Thrusters", "Actuators", "General")
    monkeypatch.setenv("DECISIONS_DOC_ID", DOC_ID)
    monkeypatch.setattr(drive_write, "build_docs", lambda: document)
    return document


def use_model(monkeypatch, *script):
    model = fake.FakeClient(list(script))
    monkeypatch.setattr(llm, "get_client", lambda: model)
    return model


def add_task(session, code="M06", owner="Sam Example", status="in_progress", **kw):
    task = Task(
        code=code,
        title=kw.pop("title", "Weld the enclosure walls"),
        subsystem=kw.pop("subsystem", "Enclosure"),
        task_type="Fabricate",
        stage="Drawing",
        status=status,
        owner=owner,
        **kw,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def add_note(session, task, member=MEMBER, did="Cut the panels.", why="", when=None, **kw):
    note = WorkNote(
        member=member["email"], member_name=member["name"], task_id=task.id,
        task_code=task.code, subsystem=task.subsystem, did=did, why=why, original=did,
        posted_at=when or utc_now(), **kw,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def stored(session, model):
    session.expire_all()
    return list(session.exec(select(model)).all())


# --- opening the panel ------------------------------------------------------------------


def test_the_top_bar_has_the_button_on_every_page_for_signed_in_members(session, as_user):
    add_task(session)
    client = as_user(MEMBER)

    for path in ("/", "/tasks", "/record", "/tasks/M06"):
        html = client.get(path).text
        assert "Log today’s work" in html and "data-worklog-open" in html, path
        assert '<dialog id="worklog"' in html, path


def test_on_a_task_page_the_panel_opens_on_that_task(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/tasks/M06").text

    assert 'data-panel-url="/worklog/panel?tab=log&amp;task=M06"' in html
    assert "data-open-url" not in html  # it opens when asked, not on every load


def test_signed_out_visitors_get_no_panel_and_no_log_routes(session, monkeypatch):
    add_task(session)
    monkeypatch.undo()

    with TestClient(main.app) as visitor:
        home = visitor.get("/")
        page = visitor.get("/worklog", follow_redirects=False)
        posted = visitor.post("/worklog/post", data={"task": "M06", "did": "x"}, follow_redirects=False)

    assert "Log today’s work" not in home.text and 'id="worklog"' not in home.text
    assert page.status_code == 303 and "/login" in page.headers["location"]
    assert posted.status_code == 303 and "/login" in posted.headers["location"]
    assert stored(session, WorkNote) == []


def test_the_log_parameter_opens_the_panel_on_load_and_only_for_a_real_code(session, as_user):
    add_task(session)
    client = as_user(MEMBER)

    opened = client.get("/tasks/M06?log=M06").text
    junk = client.get('/tasks/M06?log="><script>alert(1)</script>').text

    assert 'data-open-url="/worklog/panel?tab=log&amp;task=M06"' in opened
    assert "data-open-url" not in junk and "<script>alert(1)" not in junk


def test_the_panel_has_two_tabs_and_the_two_boxes(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/worklog/panel?task=M06").text

    assert "Log today’s work" in html and "My notebook" in html
    assert 'aria-current="true">Log today’s work' in html
    assert "What did you do?" in html and "Why did you do it that way?" in html
    assert "(recommended)" in html
    assert '<option value="M06" selected>' in html
    assert "Tidy my note" in html and "Post as written" in html


def test_the_page_version_works_without_javascript(session, as_user):
    add_task(session)

    response = as_user(MEMBER).get("/worklog?task=M06")

    assert response.status_code == 200
    assert response.text.count('id="worklog-body"') == 1  # no second copy from the dialog
    assert "What did you do?" in response.text and 'action="/worklog/post"' in response.text


def test_only_the_members_own_tasks_are_offered_with_the_asked_one_selected(session, as_user):
    add_task(session, "M06")
    add_task(session, "M07", title="Second task")
    add_task(session, "M08", owner="Riley Sample", title="Someone else's")

    html = as_user(MEMBER).get("/worklog/panel?task=M07").text

    assert '<option value="M07" selected>' in html and '<option value="M06">' in html
    assert "Someone else" not in html


def test_a_member_with_no_task_is_told_what_to_do(session, as_user):
    add_task(session, "M08", owner="Riley Sample")

    html = as_user(MEMBER).get("/worklog/panel").text

    assert "You don’t own a task yet" in html and 'name="did"' not in html


def test_a_database_failure_says_so_in_the_panel(session, as_user, monkeypatch):
    add_task(session)

    def broken(*args, **kwargs):
        raise RuntimeError("password=hunter2")

    monkeypatch.setattr(worklog, "task_choices", broken)
    response = as_user(MEMBER).get("/worklog/panel")

    assert "Couldn&#39;t load this from the database" in response.text or "Couldn’t load" in response.text
    assert "hunter2" not in response.text and "My notebook" in response.text


# --- Tidy my note --------------------------------------------------------------------------


def tidy(client, did=DID, why=WHY, **extra):
    data = {"task": "M06", "did": did, "why": why, **extra}
    return client.post("/worklog/tidy", data=data, headers=HTMX)


def test_tidy_calls_the_small_model_once_shows_the_result_and_saves_nothing(session, as_user, monkeypatch):
    add_task(session)
    model = use_model(monkeypatch, fake.reply(json.dumps(TIDIED), cost=None, input_tokens=900, output_tokens=120))

    response = tidy(as_user(MEMBER))

    assert response.status_code == 200
    assert len(model.requests) == 1 and model.requests[0]["model"] == "claude-haiku-4-5"
    html = response.text
    assert "Tidied note · check it before posting" in html
    assert TIDIED["did"] in html and TIDIED["why"] in html  # editable, in the boxes
    assert "One thing is missing:" in html and "Which two corners did you fully weld?" in html
    assert 'name="reply"' in html and 'name="asked"' in html
    assert "Post to the Crush document" in html and "Tidy again" in html
    assert f'name="original_did" value="{DID}"' in html  # their own words are kept for later
    assert stored(session, WorkNote) == [] and stored(session, DriveWrite) == []


def test_each_tidy_is_logged_with_its_cost(session, as_user, monkeypatch):
    task = add_task(session)
    use_model(monkeypatch, fake.reply(json.dumps(TIDIED), cost=None, input_tokens=900, output_tokens=120))

    tidy(as_user(MEMBER))

    (call,) = stored(session, LlmCall)
    assert call.model == "claude-haiku-4-5" and call.status == "ok"
    assert (call.input_tokens, call.output_tokens) == (900, 120)
    assert call.cost == pytest.approx((900 * 1.0 + 120 * 5.0) / 1_000_000)
    assert call.task_id == task.id and call.member == "sam@example.edu"


def test_the_model_sees_the_note_the_coach_chat_and_the_index_text_of_a_cited_source(session, as_user, monkeypatch):
    task = add_task(session)
    link = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"
    session.add(CoachMessage(task_id=task.id, member=MEMBER["email"], role="user", content="Which plate do I clamp to?"))
    session.add(
        CoachMessage(
            task_id=task.id, member="Coach", role="assistant", content="Use Fixture Plate B.",
            meta=json.dumps({"sources": [{"title": "Weld notes", "link": link, "modified": "2026-10-01"}]}),
        )
    )
    indexed = DriveFile(
        drive_id="1AbCdEfGhIjKlMnOp", name="Weld notes", mime_type="application/vnd.google-apps.document",
        kind="doc", text_status="ok", web_link=link,
    )
    session.add(indexed)
    session.commit()
    session.add(DriveChunk(file_id=indexed.id, position=0, heading="Plates", text="Fixture Plate B holds the walls."))
    session.commit()
    model = use_model(monkeypatch, fake.reply(json.dumps({**TIDIED, "question": None})))

    tidy(as_user(MEMBER))

    user_message = model.requests[0]["messages"][1]["content"]
    assert DID in user_message and "Which plate do I clamp to?" in user_message
    assert "Use Fixture Plate B." in user_message
    assert "Weld notes" in user_message and "Fixture Plate B holds the walls." in user_message


def test_answering_the_question_tidies_again_with_the_answer_and_does_not_ask_twice(session, as_user, monkeypatch):
    add_task(session)
    second = {
        "did": "Tack-welded all 4 walls and fully welded the front-left and rear-right corners.",
        "why": TIDIED["why"],
        "question": "Anything else?",
    }
    model = use_model(monkeypatch, fake.reply(json.dumps(second)))

    response = tidy(
        as_user(MEMBER), did=TIDIED["did"], why=TIDIED["why"], original_did=DID, original_why=WHY,
        asked=TIDIED["question"], reply="front-left and rear-right",
    )

    sent = model.requests[0]["messages"][1]["content"]
    assert "Earlier question: Which two corners did you fully weld?" in sent and "front-left and rear-right" in sent
    assert "front-left and rear-right corners" in response.text
    assert "One thing is missing" not in response.text
    assert f'name="original_did" value="{DID}"' in response.text  # still their first words


def test_a_rewrite_that_invents_a_detail_keeps_the_members_words_and_says_so(session, as_user, monkeypatch):
    add_task(session)
    invented = {"did": "Tack-welded 4 walls and welded 3 corners with 6082A12 clamps.", "why": WHY, "question": None}
    use_model(monkeypatch, fake.reply(json.dumps(invented)))

    html = tidy(as_user(MEMBER)).text

    assert DID in html and "6082A12" not in html
    assert "added a detail that isn’t in your note" in html


def test_a_failed_tidy_keeps_the_text_says_why_and_still_lets_them_post(session, as_user, monkeypatch):
    add_task(session)
    use_model(monkeypatch, fake.timed_out())

    response = tidy(as_user(MEMBER))

    assert response.status_code == 502
    assert "Took too long" in response.text and "post your note as written" in response.text
    assert DID in response.text and "Post as written" in response.text
    (call,) = stored(session, LlmCall)
    assert call.status == "timeout"
    assert stored(session, WorkNote) == []


def test_a_model_error_does_not_leak_anything(session, as_user, monkeypatch):
    add_task(session)
    use_model(monkeypatch, fake.server_error())

    response = tidy(as_user(MEMBER))

    assert "sk-secret-123" not in response.text and "The AI service failed" in response.text


def test_tidy_with_nothing_written_asks_for_it_without_calling_the_model(session, as_user, monkeypatch):
    add_task(session)
    model = use_model(monkeypatch)

    response = tidy(as_user(MEMBER), did="   ")

    assert response.status_code == 400 and "Write what you did first" in response.text
    assert model.requests == []


def test_tidy_has_a_daily_limit_but_posting_does_not(session, as_user, monkeypatch):
    add_task(session)
    monkeypatch.setenv("TIDY_DAILY_LIMIT", "1")
    model = use_model(monkeypatch, fake.reply(json.dumps(TIDIED)))
    client = as_user(MEMBER)

    assert tidy(client).status_code == 200
    blocked = tidy(client)

    assert blocked.status_code == 400 and "used today's 1 tidies" in blocked.text.replace("&#39;", "'")
    assert "post your note as written" in blocked.text
    assert len(model.requests) == 1


def test_tidy_works_as_a_whole_page_without_htmx(session, as_user, monkeypatch):
    add_task(session)
    use_model(monkeypatch, fake.reply(json.dumps(TIDIED)))

    response = as_user(MEMBER).post("/worklog/tidy", data={"task": "M06", "did": DID, "why": WHY})

    assert "<!doctype html>" in response.text and TIDIED["did"] in response.text


def test_the_model_is_never_called_by_opening_pages(session, as_user, monkeypatch):
    add_task(session)
    model = use_model(monkeypatch)
    client = as_user(MEMBER)

    for path in ("/", "/worklog", "/worklog/panel", "/worklog/panel?tab=notebook", "/record", "/tasks/M06?log=M06"):
        client.get(path)

    assert model.requests == []


# --- posting ----------------------------------------------------------------------------------


def post(client, **data):
    data = {"task": "M06", "did": TIDIED["did"], "why": TIDIED["why"], **data}
    return client.post("/worklog/post", data=data, headers=HTMX)


def test_posting_saves_the_note_keeps_their_own_words_and_writes_the_document(session, as_user, doc):
    task = add_task(session)

    response = post(as_user(MEMBER), original_did=DID, original_why=WHY)

    assert response.status_code == 200
    (note,) = stored(session, WorkNote)
    assert (note.member, note.member_name, note.task_id, note.task_code) == (
        "sam@example.edu", "Sam Example", task.id, "M06",
    )
    assert note.subsystem == "Enclosure"
    assert note.did == TIDIED["did"] and note.why == TIDIED["why"]
    assert note.original == f"{DID}\n\nWhy: {WHY}"
    assert note.posted_at is not None
    assert doc.lines("Enclosure") == [
        TODAY,
        f"Sam Example · M06: {TIDIED['did']} Why: {TIDIED['why']}",
    ]
    (write,) = stored(session, DriveWrite)
    assert (write.kind, write.ref_id, write.status) == ("note", note.id, "written")


def test_posting_needs_no_lead(session, as_user, doc):
    add_task(session)

    assert post(as_user(MEMBER)).status_code == 200
    assert len(stored(session, WorkNote)) == 1


def test_posting_without_tidy_keeps_the_text_as_both_note_and_original(session, as_user, doc):
    add_task(session)

    post(as_user(MEMBER), did="Sanded the lid", why="")

    (note,) = stored(session, WorkNote)
    assert note.did == "Sanded the lid" and note.why == "" and note.original == "Sanded the lid"
    assert doc.lines("Enclosure")[-1] == "Sam Example · M06: Sanded the lid."


def test_after_posting_the_notebook_shows_it_and_says_where_it_went(session, as_user, doc):
    add_task(session)

    html = post(as_user(MEMBER)).text

    assert 'aria-current="true">My notebook' in html and "data-posted" in html
    assert "Posted. Written to the Enclosure tab of the Crush document." in html
    assert TIDIED["did"] in html and f"Why: {TIDIED['why']}" in html
    assert "Retry write to Drive" not in html


def test_posting_in_a_browser_without_htmx_ends_on_the_notebook(session, as_user, doc):
    add_task(session)

    response = as_user(MEMBER).post(
        "/worklog/post", data={"task": "M06", "did": "Sanded the lid"}, follow_redirects=False
    )

    assert response.status_code == 303 and response.headers["location"] == "/worklog?tab=notebook"
    assert len(stored(session, WorkNote)) == 1


def test_the_tab_follows_the_subsystem_and_says_when_it_used_general(session, as_user, doc):
    add_task(session, "T01", subsystem="Torpedo", title="Torpedo body")
    add_task(session, "X01", subsystem="Electronics Mounts", title="Board tray")
    doc.tabs[:] = [t for t in doc.tabs if t.title != "Actuators"]
    client = as_user(MEMBER)

    html = post(client, task="T01", did="Printed the body").text

    assert "no Torpedo tab, so it went under General" in html
    assert TODAY in doc.lines("General") and "Printed the body" in doc.lines("General")[-1]
    post(client, task="X01", did="Fitted the tray")
    assert "Fitted the tray" in doc.lines("Enclosure")[-1]  # no tab of its own: its part of Crush


def test_nothing_to_post_is_refused_and_the_text_stays(session, as_user, doc):
    add_task(session)

    response = post(as_user(MEMBER), did="  ", why="because")

    assert response.status_code == 400 and "Write what you did before posting" in response.text
    assert "because" in response.text
    assert stored(session, WorkNote) == [] and doc.gets == 0


def test_an_unknown_task_is_refused(session, as_user, doc):
    response = post(as_user(MEMBER), task="ZZ99")

    assert response.status_code == 400 and "Choose which task" in response.text
    assert stored(session, WorkNote) == []


def test_pressing_post_twice_makes_one_note_and_one_line_in_the_document(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)

    post(client)
    post(client)

    assert len(stored(session, WorkNote)) == 1
    assert doc.tab("Enclosure").text().count("Tack-welded") == 1


def test_a_note_is_markup_safe_in_every_page_it_appears_on(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)

    html = post(client, did='<script>alert("x")</script> welded', why="<b>x</b>").text

    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "<script>alert" not in client.get("/tasks/M06").text
    assert "<script>alert" not in client.get("/record?notes=1").text


# --- when the document write fails -------------------------------------------------------------


def test_a_failed_write_keeps_the_note_and_offers_a_retry(session, as_user, doc):
    add_task(session)
    doc.fail_get = http_error(503, "unavailable secret-token-123")
    client = as_user(MEMBER)

    response = post(client)

    assert response.status_code == 200
    assert len(stored(session, WorkNote)) == 1
    (write,) = stored(session, DriveWrite)
    assert write.status == "failed"
    html = response.text
    assert "Saved in your notebook." in html and "Google Docs didn" in html
    assert "Retry write to Drive" in html and "secret-token-123" not in html
    # The notebook says the same on a later visit.
    assert "Retry write to Drive" in client.get("/worklog/panel?tab=notebook").text


def test_retry_writes_it_once_and_the_button_goes(session, as_user, doc):
    add_task(session)
    doc.fail_get = http_error(503, "unavailable")
    client = as_user(MEMBER)
    post(client)
    (note,) = stored(session, WorkNote)
    doc.fail_get = None

    response = client.post(f"/worklog/notes/{note.id}/retry", headers=HTMX)
    client.post(f"/worklog/notes/{note.id}/retry", headers=HTMX)  # a second press

    assert "Posted. Written to the Enclosure tab" in response.text
    assert "Retry write to Drive" not in response.text
    assert doc.tab("Enclosure").text().count("Tack-welded") == 1
    assert stored(session, DriveWrite)[0].status == "written"


def test_retry_that_fails_again_says_so_and_keeps_the_button(session, as_user, doc):
    add_task(session)
    doc.fail_get = http_error(403, "no")
    client = as_user(MEMBER)
    post(client)
    (note,) = stored(session, WorkNote)

    response = client.post(f"/worklog/notes/{note.id}/retry", headers=HTMX)

    assert "editor" in response.text and "Retry write to Drive" in response.text
    assert stored(session, DriveWrite)[0].attempts == 2


def test_without_a_configured_document_the_note_is_kept_and_says_so(session, as_user, monkeypatch):
    add_task(session)
    monkeypatch.delenv("DECISIONS_DOC_ID", raising=False)

    html = post(as_user(MEMBER)).text

    assert len(stored(session, WorkNote)) == 1
    assert "isn&#39;t set up yet" in html or "isn't set up yet" in html


def test_only_the_author_or_a_lead_can_retry_a_note(session, as_user, doc):
    task = add_task(session)
    note = add_note(session, task)

    stranger = as_user(OTHER).post(f"/worklog/notes/{note.id}/retry", headers=HTMX)
    assert stranger.status_code == 404
    assert doc.gets == 0

    lead = as_user(LEAD).post(f"/worklog/notes/{note.id}/retry", headers=HTMX)
    assert lead.status_code == 200 and "Cut the panels." in doc.tab("Enclosure").text()


# --- the notebook ---------------------------------------------------------------------------------


def test_the_notebook_has_only_my_notes_newest_first_grouped_by_day(session, as_user, doc):
    task = add_task(session)
    add_note(session, task, did="Older work.", when=utc_now() - timedelta(days=3))
    add_note(session, task, did="Newer work.")
    add_note(session, task, member=OTHER, did="Riley's secret work.")

    html = as_user(MEMBER).get("/worklog/panel?tab=notebook").text

    assert html.index("Newer work.") < html.index("Older work.")
    assert "Riley" not in html
    assert html.count('class="day"') == 2


def test_an_empty_notebook_says_so(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/worklog/panel?tab=notebook").text

    assert "Nothing in your notebook yet" in html


# --- the Home banner ----------------------------------------------------------------------------------


def active(session, task, who=MEMBER):
    session.add(
        TaskEvent(task_id=task.id, actor=who["name"], from_status="todo", to_status="in_progress",
                  kind="start", created_at=utc_now())
    )
    session.commit()


def test_the_banner_names_the_task_the_member_worked_on_today(session, as_user):
    task = add_task(session)
    active(session, task)

    html = as_user(MEMBER).get("/").text

    assert 'class="banner"' in html and "You worked on" in html and "M06" in html
    assert "Not today" in html and 'data-panel-url="/worklog/panel?tab=log&amp;task=M06"' in html


def test_asking_the_coach_counts_as_being_active(session, as_user):
    task = add_task(session)
    session.add(CoachMessage(task_id=task.id, member=MEMBER["email"], role="user", content="Which rod?"))
    session.commit()

    assert 'class="banner"' in as_user(MEMBER).get("/").text


def test_no_banner_when_nothing_happened_today(session, as_user):
    task = add_task(session)
    session.add(
        TaskEvent(task_id=task.id, actor=MEMBER["name"], from_status="todo", to_status="in_progress",
                  kind="start", created_at=utc_now() - timedelta(days=2))
    )
    session.commit()

    assert 'class="banner"' not in as_user(MEMBER).get("/").text


def test_no_banner_for_someone_elses_activity(session, as_user):
    task = add_task(session)
    active(session, task, who=OTHER)

    assert 'class="banner"' not in as_user(MEMBER).get("/").text


def test_no_banner_once_they_have_logged_that_task_today(session, as_user):
    task = add_task(session)
    active(session, task)
    add_note(session, task)

    assert 'class="banner"' not in as_user(MEMBER).get("/").text


def test_logging_another_task_leaves_the_banner_for_this_one(session, as_user):
    one, two = add_task(session, "M06"), add_task(session, "M07", title="Second")
    active(session, one)
    active(session, two)
    add_note(session, two)

    html = as_user(MEMBER).get("/").text

    assert 'class="banner"' in html and "M06" in html.split('class="banner"')[1].split("</div>")[0]


def test_not_today_hides_the_banner_until_tomorrow(session, as_user, monkeypatch):
    task = add_task(session)
    active(session, task)
    client = as_user(MEMBER)
    assert 'class="banner"' in client.get("/").text

    response = client.post("/worklog/dismiss", data={"next": "/"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert 'class="banner"' not in client.get("/").text

    tomorrow = localtime.today() + timedelta(days=1)
    monkeypatch.setattr(localtime, "today", lambda: tomorrow)
    assert 'class="banner"' in client.get("/").text


def test_not_today_cannot_send_you_to_another_site(session, as_user):
    response = as_user(MEMBER).post("/worklog/dismiss", data={"next": "https://evil.example/"}, follow_redirects=False)

    assert response.headers["location"] == "/"


def test_the_signed_out_preview_has_no_banner(session, monkeypatch):
    task = add_task(session)
    active(session, task)
    monkeypatch.undo()

    with TestClient(main.app) as visitor:
        assert 'class="banner"' not in visitor.get("/").text


def test_a_failure_checking_the_banner_leaves_the_rest_of_home(session, as_user, monkeypatch):
    add_task(session)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(worklog, "active_task_today", broken)
    response = as_user(MEMBER).get("/")

    assert response.status_code == 200 and "Your task" in response.text


# --- after "Mark step done" -------------------------------------------------------------------------------


def test_mark_step_done_opens_the_log_on_that_task(session, as_user):
    add_task(session)
    client = as_user(MEMBER)

    from_task_page = client.post(
        "/tasks/M06/status", data={"action": "advance", "step": "Drawing"}, follow_redirects=False
    )

    assert from_task_page.status_code == 303
    assert from_task_page.headers["location"] == "/tasks/M06?log=M06"
    page = client.get(from_task_page.headers["location"]).text
    assert 'data-open-url="/worklog/panel?tab=log&amp;task=M06"' in page


def test_mark_step_done_from_home_opens_it_there(session, as_user):
    add_task(session)

    response = as_user(MEMBER).post(
        "/tasks/M06/status", data={"action": "advance", "step": "Drawing", "next": "/"}, follow_redirects=False
    )

    assert response.headers["location"] == "/?log=M06"


@pytest.mark.parametrize("action, status", [("unclaim", "in_progress"), ("start", "todo")])
def test_other_moves_do_not_open_it(session, as_user, action, status):
    add_task(session, status=status)

    response = as_user(MEMBER).post("/tasks/M06/status", data={"action": action}, follow_redirects=False)

    assert response.headers["location"] == "/tasks/M06"


def test_a_refused_move_does_not_open_it(session, as_user):
    add_task(session, owner="Riley Sample")

    response = as_user(MEMBER).post(
        "/tasks/M06/status", data={"action": "advance", "step": "Drawing"}, follow_redirects=False
    )

    assert response.status_code == 403 and "log=" not in response.headers.get("location", "")


# --- History and the Record --------------------------------------------------------------------------------------


def test_a_note_shows_in_the_tasks_history_among_the_moves(session, as_user):
    task = add_task(session)
    session.add(
        TaskEvent(task_id=task.id, actor="Sam Example", from_status="todo", to_status="in_progress",
                  kind="start", created_at=utc_now() - timedelta(hours=2))
    )
    session.commit()
    add_note(session, task, did="Cut the panels.", why="rev C moved the latch holes")

    html = as_user(MEMBER).get("/tasks/M06").text

    history = html.split('id="history-heading"')[1]
    assert "Sam Example logged work" in history
    assert "Cut the panels. Why: rev C moved the latch holes" in history
    assert history.index("logged work") < history.index("started the task")  # newest first


def test_the_record_hides_notes_until_asked_and_the_toggle_appears_with_the_first_note(session, as_user):
    task = add_task(session)
    client = as_user(MEMBER)
    assert "work notes" not in client.get("/record").text

    add_note(session, task, did="Cut and deburred the panels.", why="rev C")
    default = client.get("/record").text
    shown = client.get("/record?notes=1").text

    assert "Show work notes" in default and "Cut and deburred" not in default
    assert "Hide work notes" in shown and "Cut and deburred the panels." in shown
    assert '<span class="tag tag--note">Note</span>Sam Example · <a class="code" href="/tasks/M06">M06</a>' in shown
    assert "Why: rev C" in shown


def test_notes_and_answers_share_the_days_newest_first(session, as_user):
    task = add_task(session)
    add_note(session, task, did="Morning work.", when=utc_now() - timedelta(hours=3))
    session.add(
        Decision(subsystem="Enclosure", topic="Latch", decision="Use 6082A12", rationale="", status="confirmed",
                 proposed_by="Lee Lead", confirmed_by="Lee Lead", confirmed_at=utc_now())
    )
    session.commit()

    html = as_user(MEMBER).get("/record?notes=1").text

    assert html.index("Use 6082A12") < html.index("Morning work.")


def test_the_record_keeps_notes_to_their_part_of_crush_and_the_search_applies(session, as_user):
    task = add_task(session)
    thruster = add_task(session, "M09", subsystem="Thrusters", title="Thruster mount")
    add_note(session, task, did="Welded the walls.")
    add_note(session, task, did="Sanded the lid.")
    add_note(session, thruster, did="Drilled the mount.")
    client = as_user(MEMBER)

    walls = client.get("/record?notes=1&q=walls").text
    thrusters = client.get("/record?part=thrusters&notes=1").text

    assert "Welded the walls." in walls and "Sanded the lid." not in walls and "Drilled" not in walls
    assert "Drilled the mount." in thrusters and "Welded" not in thrusters
    assert 'notes=1' in client.get("/record?notes=1").text.split('aria-label="Part of Crush"')[1].split("</nav>")[0]


def test_a_note_in_the_record_says_whether_it_reached_the_document(session, as_user, doc):
    task = add_task(session)
    good, bad = add_note(session, task, did="Written one."), add_note(session, task, did="Unwritten one.")
    drive_write.write_note(session, good)
    doc.fail_get = http_error(503, "x")
    drive_write.write_note(session, bad)

    html = as_user(MEMBER).get("/record?notes=1").text

    assert "Written to the Enclosure tab of the Crush document." in html
    assert "write--failed" in html
    assert f'href="https://docs.google.com/document/d/{DOC_ID}/edit?tab=t.0"' in html
    assert f'href="https://docs.google.com/document/d/{DOC_ID}/edit" target' in html  # the panel's link


def test_removing_a_task_keeps_the_notes_in_the_notebook(session, as_user):
    import task_removal

    task = add_task(session)
    note = add_note(session, task)
    preview = task_removal.preview(session, ["M06"])

    task_removal.remove(session, ["M06"], "lee@example.edu", preview.fingerprint)

    kept = stored(session, WorkNote)
    assert [n.id for n in kept] == [note.id] and kept[0].task_id is None and kept[0].task_code == "M06"
    assert "Cut the panels." in as_user(MEMBER).get("/worklog/panel?tab=notebook").text


def test_the_work_notes_text_in_the_prompt_files_exists():
    assert "Never invent" in llm.tidy_prompt()
    assert re.search(r"one short question", llm.tidy_prompt())


# --- recorded answers go to the Crush document when a lead records them ------------------------------------


def add_request(session, task):
    from models import AnswerRequest

    request = AnswerRequest(
        task_id=task.id, task_code=task.code, subsystem="Enclosure", question="Baseplate gasket hole clearance",
        asked_by="Sam Example", kind="conflict", answer_text="The sources disagree.",
        options=[
            {"value": "0.050 in", "source": "Crush Documentation",
             "link": "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit", "date": "2026-03-30"},
            {"value": "0.030 in", "source": "Seal Calculations", "link": "", "date": "2026-02-10"},
        ],
        sources=[], suggested=0,
    )
    session.add(request)
    session.commit()
    return request


def test_recording_an_answer_writes_it_to_the_document_with_everything_the_record_has(session, as_user, doc):
    task = add_task(session, "M03", status="needs_review")
    old = Decision(
        subsystem="Enclosure", topic="Baseplate gasket hole clearance", decision="0.030 in", rationale="",
        status="confirmed", proposed_by="Lee Lead", confirmed_by="Lee Lead", confirmed_at=utc_now() - timedelta(days=9),
    )
    session.add(old)
    session.commit()
    request = add_request(session, task)

    response = as_user(LEAD).post(f"/answers/{request.id}/record", data={"choice": "0"}, follow_redirects=False)

    assert response.status_code == 303
    assert doc.lines("Enclosure") == [
        TODAY,
        "Baseplate gasket hole clearance: 0.050 in",
        "Why: From Crush Documentation, 2026-03-30.",
        "Replaces: 0.030 in",
        "Sources: Crush Documentation",
        "Recorded by Lee Lead · M03",
    ]
    assert doc.bold_text("Enclosure") == "Baseplate gasket hole clearance: 0.050 in"
    answer = next(w for w in stored(session, DriveWrite) if w.kind == "answer")
    assert answer.status == "written" and answer.tab_title == "Enclosure"


def test_an_answer_that_is_not_recorded_is_not_written(session, as_user, doc):
    task = add_task(session, "M03", status="needs_review")
    request = add_request(session, task)

    as_user(LEAD).post(f"/answers/{request.id}/defer", follow_redirects=False)
    as_user(MEMBER).post(f"/answers/{request.id}/record", data={"choice": "0"}, follow_redirects=False)

    assert doc.gets == 0 and stored(session, DriveWrite) == []


def test_accepting_work_with_a_line_records_and_writes_it_but_without_one_writes_nothing(session, as_user, doc):
    add_task(session, "M03", status="needs_review", owner="Riley Sample")
    add_task(session, "M04", status="needs_review", owner="Riley Sample")
    client = as_user(LEAD)

    client.post("/tasks/M04/status", data={"action": "approve"}, follow_redirects=False)
    assert doc.gets == 0

    client.post("/tasks/M03/status", data={"action": "approve", "record": "Panels are 3 mm thick"}, follow_redirects=False)

    assert doc.lines("Enclosure")[1] == "Weld the enclosure walls: Panels are 3 mm thick"
    assert doc.lines("Enclosure")[-1] == "Recorded by Lee Lead · M03"


def test_a_failed_write_never_stops_an_answer_being_recorded_and_the_lead_can_retry(session, as_user, doc):
    task = add_task(session, "M03", status="needs_review")
    request = add_request(session, task)
    doc.fail_get = http_error(503, "unavailable secret-token-123")
    client = as_user(LEAD)

    response = client.post(f"/answers/{request.id}/record", data={"choice": "0"}, follow_redirects=False)

    assert response.status_code == 303
    (decision,) = stored(session, Decision)
    assert decision.status == "confirmed"
    record_page = client.get("/record").text
    assert "Baseplate gasket hole clearance:" in record_page and "0.050 in" in record_page
    assert "Google Docs didn" in record_page and "secret-token-123" not in record_page
    assert "Retry write to Drive" in record_page
    doc.fail_get = None

    retried = client.post(f"/worklog/answers/{decision.id}/retry", data={"part": "enclosure"}, follow_redirects=False)

    assert retried.status_code == 303 and retried.headers["location"] == f"/record?part=enclosure#answer-{decision.id}"
    assert doc.tab("Enclosure").text().count("Baseplate gasket hole clearance: 0.050 in") == 1
    assert "Retry write to Drive" not in client.get("/record").text


def test_members_cannot_retry_an_answers_write_and_see_no_button(session, as_user, doc):
    decision = Decision(subsystem="Enclosure", topic="T", decision="A", rationale="", status="confirmed",
                        proposed_by="Lee Lead", confirmed_by="Lee Lead", confirmed_at=utc_now())
    session.add(decision)
    session.commit()
    client = as_user(MEMBER)

    assert client.post(f"/worklog/answers/{decision.id}/retry").status_code == 403
    assert "Retry write to Drive" not in client.get("/record").text
    assert doc.gets == 0


def test_an_answer_goes_under_the_tab_for_its_subsystem_even_if_it_is_a_part_tab(session, as_user, doc):
    task = add_task(session, "T01", subsystem="Torpedo", status="needs_review")
    session.add(task)
    request = add_request(session, task)
    request.subsystem = "Torpedo"
    session.add(request)
    session.commit()

    as_user(LEAD).post(f"/answers/{request.id}/record", data={"choice": "1"}, follow_redirects=False)

    assert TODAY in doc.lines("Actuators") and "Baseplate gasket hole clearance: 0.030 in" in doc.lines("Actuators")


def test_a_lead_cannot_use_retry_to_write_an_answer_that_is_not_recorded(session, as_user, doc):
    decision = Decision(subsystem="Enclosure", topic="T", decision="Maybe", rationale="", status="proposed",
                        proposed_by="Sam Example")
    session.add(decision)
    session.commit()

    response = as_user(LEAD).post(f"/worklog/answers/{decision.id}/retry", follow_redirects=False)

    assert response.status_code == 303
    assert doc.gets == 0 and stored(session, DriveWrite) == []
