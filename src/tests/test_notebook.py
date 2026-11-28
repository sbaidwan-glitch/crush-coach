"""The Notebook: the side panel and its three tabs, Tidy my note (one model call, on the
button), publishing without a lead, the Crush document write and its retry, the leaving nudge,
and where entries show up (History, Record). Made-up people and tasks; the model and Google
are fakes. Photos, specs, private notes and the plan button have their own files."""

import json
import re
from datetime import timedelta
from pathlib import Path

import fake_openai as fake
import pytest
from fake_crush_doc import DOC_ID
from fake_google import http_error
from fastapi.testclient import TestClient
from notebook_helpers import (
    HTMX,
    LEAD,
    MEMBER,
    OTHER,
    TODAY,
    add_note,
    add_task,
    stored,
    use_model,
)

import drive_write
import llm
import localtime
import main
import notebook
from models import (
    CoachMessage,
    Decision,
    DriveChunk,
    DriveFile,
    DriveWrite,
    LlmCall,
    PrivateNote,
    TaskEvent,
    WorkNote,
    utc_now,
)

DID = "tacked the 4 walls and welded 2 corners, one warped a bit so clamped it to the fixture plate"
WHY = "did opposite corners first so the heat pulls evenly"
TIDIED = {
    "did": "Tack-welded all 4 walls and fully welded 2 corners. One warped slightly, so it was clamped to the fixture plate.",
    "why": "Welded opposite corners first so the heat pulls evenly.",
    "question": "Which two corners did you fully weld?",
}


# --- opening the panel ------------------------------------------------------------------


def test_the_top_bar_has_the_button_on_every_page_for_signed_in_members(session, as_user):
    add_task(session)
    client = as_user(MEMBER)

    for path in ("/", "/tasks", "/record", "/tasks/M06"):
        html = client.get(path).text
        # Home has "+ Log today's work" in the place of the Notebook button; same link.
        assert (">Notebook</a>" in html or "Log today" in html) and "data-notebook-open" in html, path
        assert '<dialog id="notebook"' in html and "Open full page" in html, path


def test_on_a_task_page_the_panel_opens_on_that_task(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/tasks/M06").text

    assert 'data-panel-url="/notebook/panel?tab=today&amp;task=M06"' in html
    assert "data-open-url" not in html  # it opens when asked, not on every load


def test_signed_out_visitors_get_no_panel_and_see_nothing_of_the_notebook(session, monkeypatch):
    add_task(session)
    monkeypatch.undo()

    with TestClient(main.app) as visitor:
        home = visitor.get("/")
        page = visitor.get("/notebook")
        panel = visitor.get("/notebook/panel", follow_redirects=False)
        entry = visitor.post("/notebook/entry/publish", data={"task": "M06", "did": "x"}, follow_redirects=False)
        private = visitor.post("/notebook/private", data={"text": "x"}, follow_redirects=False)

    assert ">Notebook</a>" not in home.text and 'id="notebook"' not in home.text
    assert page.status_code == 401 and "Sign in" in page.text and 'name="did"' not in page.text
    for response in (panel, entry, private):
        assert response.status_code == 303 and "/login" in response.headers["location"]
    assert stored(session, WorkNote) == [] and stored(session, PrivateNote) == []


def test_the_log_parameter_no_longer_opens_the_panel(session, as_user):
    add_task(session)
    client = as_user(MEMBER)

    for path in ("/tasks/M06?log=M06", '/tasks/M06?log="><script>alert(1)</script>'):
        html = client.get(path).text
        assert "data-open-url" not in html and "<script>alert(1)" not in html


def test_the_panel_has_three_tabs_and_the_entry_form(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/notebook/panel?task=M06").text

    for tab in ("Today’s entry", "Earlier", "Private notes"):
        assert tab in html
    assert 'aria-current="true">Today’s entry' in html
    assert "What I did" in html and "Why <span" in html and "(recommended)" in html
    assert '<option value="M06" selected>' in html
    for button in ("Tidy my note", "Suggest specs", "Save draft", "Publish to Crush doc"):
        assert button in html
    assert "Add photos" in html and 'capture="environment"' in html and 'type="file"' in html
    assert 'action="/notebook/entry/publish"' in html and 'enctype="multipart/form-data"' in html


def test_the_page_version_works_without_javascript(session, as_user):
    add_task(session)

    response = as_user(MEMBER).get("/notebook?task=M06")

    assert response.status_code == 200
    html = response.text
    assert html.count('id="notebook-body"') == 0 and html.count('<dialog') == 0  # no second copy
    assert "What I did" in html and 'action="/notebook/entry/publish"' in html
    assert "hx-post" not in html  # plain posts, no htmx needed
    # Compose on the left, history on the right: one grid that stacks on a phone.
    assert html.index("Today’s entry") < html.index("Earlier") and "grid-2 notebook-page" in html


def test_only_the_members_own_tasks_are_offered_with_the_asked_one_selected(session, as_user):
    add_task(session, "M06")
    add_task(session, "M07", title="Second task")
    add_task(session, "M08", owner="Riley Sample", title="Someone else's")

    html = as_user(MEMBER).get("/notebook/panel?task=M07").text

    assert '<option value="M07" selected>' in html and '<option value="M06">' in html
    assert "Someone else" not in html


def test_a_member_with_no_task_is_told_what_to_do(session, as_user):
    add_task(session, "M08", owner="Riley Sample")

    html = as_user(MEMBER).get("/notebook/panel").text

    assert "You don’t own a task yet" in html and 'name="did"' not in html


def test_a_database_failure_says_so_in_the_panel(session, as_user, monkeypatch):
    add_task(session)

    def broken(*args, **kwargs):
        raise RuntimeError("password=hunter2")

    monkeypatch.setattr(notebook, "task_choices", broken)
    response = as_user(MEMBER).get("/notebook/panel")

    assert "Couldn&#39;t load this from the database" in response.text or "Couldn’t load" in response.text
    assert "hunter2" not in response.text and "Earlier" in response.text


# --- Tidy my note --------------------------------------------------------------------------


def tidy(client, did=DID, why=WHY, **extra):
    data = {"task": "M06", "did": did, "why": why, **extra}
    return client.post("/notebook/entry/tidy", data=data, headers=HTMX)


def test_tidy_calls_the_small_model_once_shows_the_result_and_saves_nothing(session, as_user, monkeypatch):
    add_task(session)
    model = use_model(monkeypatch, fake.reply(json.dumps(TIDIED), cost=None, input_tokens=900, output_tokens=120))

    response = tidy(as_user(MEMBER))

    assert response.status_code == 200
    assert len(model.requests) == 1 and model.requests[0]["model"] == "claude-haiku-4-5"
    html = response.text
    assert "Tidied note · check it before you publish" in html
    assert TIDIED["did"] in html and TIDIED["why"] in html  # editable, in the boxes
    assert "One thing is missing:" in html and "Which two corners did you fully weld?" in html
    assert 'name="reply"' in html and 'name="asked"' in html
    assert "Publish to Crush doc" in html and "Tidy again" in html
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


def test_a_failed_tidy_keeps_the_text_says_why_and_still_lets_them_publish(session, as_user, monkeypatch):
    add_task(session)
    use_model(monkeypatch, fake.timed_out())

    response = tidy(as_user(MEMBER))

    assert response.status_code == 502
    assert "Took too long" in response.text and "publish your note as written" in response.text
    assert DID in response.text and "Publish to Crush doc" in response.text
    (call,) = stored(session, LlmCall)
    assert call.status == "timeout" and call.purpose == "tidy"
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
    assert "publish your note as written" in blocked.text
    assert len(model.requests) == 1


def test_tidy_works_as_a_whole_page_without_htmx(session, as_user, monkeypatch):
    add_task(session)
    use_model(monkeypatch, fake.reply(json.dumps(TIDIED)))

    response = as_user(MEMBER).post("/notebook/entry/tidy", data={"task": "M06", "did": DID, "why": WHY})

    assert "<!doctype html>" in response.text and TIDIED["did"] in response.text


def test_the_model_is_never_called_by_opening_pages(session, as_user, monkeypatch):
    add_task(session)
    model = use_model(monkeypatch)
    client = as_user(MEMBER)

    for path in ("/", "/worklog", "/notebook/panel", "/notebook/panel?tab=earlier", "/record", "/tasks/M06"):
        client.get(path)

    assert model.requests == []


# --- posting ----------------------------------------------------------------------------------


def post(client, **data):
    data = {"task": "M06", "did": TIDIED["did"], "why": TIDIED["why"], **data}
    return client.post("/notebook/entry/publish", data=data, headers=HTMX)


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


def test_after_publishing_earlier_shows_it_with_its_state_and_where_it_went(session, as_user, doc):
    add_task(session)

    html = post(as_user(MEMBER)).text

    assert 'aria-current="true">Earlier' in html and "data-posted" in html
    assert "Published. Written to the Enclosure tab of the Crush document." in html
    assert TIDIED["did"] in html and f"Why: {TIDIED['why']}" in html
    assert '<span class="status status--done">Published</span>' in html  # a mark and the word
    assert "Open in doc" in html and "Add a follow-up" in html
    assert "Retry" not in html and "Edit draft" not in html


def test_publishing_in_a_browser_without_htmx_ends_on_earlier_and_says_where_it_went(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)

    response = client.post(
        "/notebook/entry/publish", data={"task": "M06", "did": "Sanded the lid"}, follow_redirects=False
    )

    assert response.status_code == 303 and response.headers["location"] == "/notebook?tab=earlier"
    assert len(stored(session, WorkNote)) == 1
    page = client.get(response.headers["location"])
    assert "Published. Written to the Enclosure tab" in page.text and "Sanded the lid" in page.text
    assert "Published. Written to" not in client.get("/notebook?tab=earlier").text  # said once


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

    assert response.status_code == 400 and "Write what you did before publishing" in response.text
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


def test_a_failed_write_keeps_the_entry_marks_it_failed_and_offers_a_retry(session, as_user, doc):
    add_task(session)
    doc.fail_get = http_error(503, "unavailable secret-token-123")
    client = as_user(MEMBER)

    response = post(client)

    assert response.status_code == 200
    assert len(stored(session, WorkNote)) == 1
    (write,) = stored(session, DriveWrite)
    assert write.status == "failed"
    html = response.text
    assert "didn’t reach the Crush document" in html and "Google Docs didn" in html
    assert '<span class="status status--review">Failed to publish</span>' in html
    assert "Retry" in html and "secret-token-123" not in html
    assert "Open in doc" not in html
    # Earlier says the same on a later visit.
    later = client.get("/notebook/panel?tab=earlier").text
    assert "Failed to publish" in later and "/retry" in later


def test_retry_writes_it_once_and_the_button_goes(session, as_user, doc):
    add_task(session)
    doc.fail_get = http_error(503, "unavailable")
    client = as_user(MEMBER)
    post(client)
    (note,) = stored(session, WorkNote)
    doc.fail_get = None

    response = client.post(f"/notebook/entries/{note.id}/retry", headers=HTMX)
    client.post(f"/notebook/entries/{note.id}/retry", headers=HTMX)  # a second press

    assert "Published. Written to the Enclosure tab" in response.text
    assert "Retry" not in response.text
    assert doc.tab("Enclosure").text().count("Tack-welded") == 1
    assert stored(session, DriveWrite)[0].status == "written"


def test_retry_that_fails_again_says_so_and_keeps_the_button(session, as_user, doc):
    add_task(session)
    doc.fail_get = http_error(403, "no")
    client = as_user(MEMBER)
    post(client)
    (note,) = stored(session, WorkNote)

    response = client.post(f"/notebook/entries/{note.id}/retry", headers=HTMX)

    assert "editor" in response.text and "Retry" in response.text
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

    stranger = as_user(OTHER).post(f"/notebook/entries/{note.id}/retry", headers=HTMX)
    assert stranger.status_code == 404
    assert doc.gets == 0

    lead = as_user(LEAD).post(f"/notebook/entries/{note.id}/retry", headers=HTMX)
    assert lead.status_code == 200 and "Cut the panels." in doc.tab("Enclosure").text()


# --- the notebook ---------------------------------------------------------------------------------


def test_the_notebook_has_only_my_notes_newest_first_grouped_by_day(session, as_user, doc):
    task = add_task(session)
    add_note(session, task, did="Older work.", when=utc_now() - timedelta(days=3))
    add_note(session, task, did="Newer work.")
    add_note(session, task, member=OTHER, did="Riley's secret work.")

    html = as_user(MEMBER).get("/notebook/panel?tab=earlier").text

    assert html.index("Newer work.") < html.index("Older work.")
    assert "Riley" not in html
    assert html.count('class="day"') == 2


def test_an_empty_notebook_says_so(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/notebook/panel?tab=earlier").text

    assert "Nothing in your notebook yet" in html


# --- the leaving nudge (rendered hidden on every signed-in page; notebook.js shows it) ----------------

NUDGE = 'id="notebook-nudge"'


def active(session, task, who=MEMBER):
    session.add(
        TaskEvent(task_id=task.id, actor=who["name"], from_status="todo", to_status="in_progress",
                  kind="start", created_at=utc_now())
    )
    session.commit()


def nudge_html(html: str) -> str:
    return html[html.index(NUDGE) : html.index("</aside>", html.index(NUDGE))]


def test_the_nudge_names_the_task_the_member_worked_on_today_and_starts_hidden(session, as_user):
    task = add_task(session)
    active(session, task)

    html = as_user(MEMBER).get("/").text

    nudge = nudge_html(html)
    assert 'role="status"' in nudge and " hidden " in nudge  # shown only by notebook.js
    assert "You worked on" in nudge and "M06" in nudge
    assert "Write today’s entry" in nudge and "Not today" in nudge
    assert 'data-panel-url="/notebook/panel?tab=today&amp;task=M06"' in nudge
    assert 'action="/notebook/dismiss"' in nudge and 'name="next" value="/"' in nudge
    assert f'data-key="cc-nudge:{MEMBER["email"]}:{localtime.today().isoformat()}"' in nudge
    assert "autofocus" not in nudge


def test_the_nudge_is_on_every_signed_in_page_but_no_longer_sits_on_home_as_a_banner(session, as_user):
    task = add_task(session)
    active(session, task)
    client = as_user(MEMBER)

    for path in ("/", "/tasks", "/tasks/M06", "/record", "/timeline"):
        html = client.get(path).text
        assert NUDGE in html and html.count(NUDGE) == 1, path
        assert 'class="banner' not in html, path
    # It names where "Not today" should send the member back to.
    assert 'name="next" value="/tasks"' in nudge_html(client.get("/tasks").text)


def test_the_nudge_is_not_on_the_notebook_page_or_in_htmx_fragments(session, as_user):
    task = add_task(session)
    active(session, task)
    client = as_user(MEMBER)

    assert NUDGE not in client.get("/notebook").text
    assert NUDGE not in client.get("/notebook/panel?tab=today", headers={"HX-Request": "true"}).text


def test_home_has_no_bottom_notebook_banner(session, as_user):
    html = as_user(MEMBER).get("/").text

    assert "Make today" not in html and "Open notebook" not in html
    # The top-bar button and the sidebar link stay.
    assert "Log today" in html and 'href="/notebook"' in html


def test_asking_the_coach_counts_as_being_active(session, as_user):
    task = add_task(session)
    session.add(CoachMessage(task_id=task.id, member=MEMBER["email"], role="user", content="Which rod?"))
    session.commit()

    assert NUDGE in as_user(MEMBER).get("/").text


def test_no_nudge_when_nothing_happened_today(session, as_user):
    task = add_task(session)
    session.add(
        TaskEvent(task_id=task.id, actor=MEMBER["name"], from_status="todo", to_status="in_progress",
                  kind="start", created_at=utc_now() - timedelta(days=2))
    )
    session.commit()

    for path in ("/", "/tasks"):
        assert NUDGE not in as_user(MEMBER).get(path).text


def test_no_nudge_for_someone_elses_activity(session, as_user):
    task = add_task(session)
    active(session, task, who=OTHER)

    assert NUDGE not in as_user(MEMBER).get("/").text


def test_no_nudge_once_they_have_logged_that_task_today(session, as_user):
    task = add_task(session)
    active(session, task)
    add_note(session, task)

    assert NUDGE not in as_user(MEMBER).get("/").text


def test_logging_another_task_leaves_the_nudge_for_this_one(session, as_user):
    one, two = add_task(session, "M06"), add_task(session, "M07", title="Second")
    active(session, one)
    active(session, two)
    add_note(session, two)

    nudge = nudge_html(as_user(MEMBER).get("/").text)

    assert "M06" in nudge and "M07" not in nudge


def test_not_today_hides_the_nudge_on_every_page_until_tomorrow(session, as_user, monkeypatch):
    task = add_task(session)
    active(session, task)
    client = as_user(MEMBER)
    assert NUDGE in client.get("/").text

    response = client.post("/notebook/dismiss", data={"next": "/tasks"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/tasks"
    for path in ("/", "/tasks", "/record"):
        assert NUDGE not in client.get(path).text, path

    tomorrow = localtime.today() + timedelta(days=1)
    monkeypatch.setattr(localtime, "today", lambda: tomorrow)
    assert NUDGE in client.get("/").text


def test_not_today_cannot_send_you_to_another_site(session, as_user):
    response = as_user(MEMBER).post("/notebook/dismiss", data={"next": "https://evil.example/"}, follow_redirects=False)

    assert response.headers["location"] == "/"


def test_signed_out_visitors_get_no_nudge_on_any_page(session, monkeypatch):
    task = add_task(session)
    active(session, task)
    monkeypatch.undo()

    with TestClient(main.app) as visitor:
        for path in ("/", "/tasks", "/timeline", "/record"):
            assert NUDGE not in visitor.get(path).text, path


def test_a_failure_checking_the_nudge_leaves_the_page(session, as_user, monkeypatch):
    add_task(session)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(notebook, "active_task_today", broken)
    for path in ("/", "/tasks"):
        response = as_user(MEMBER).get(path)
        assert response.status_code == 200 and NUDGE not in response.text


def test_the_nudge_script_follows_the_rules_in_the_brief():
    """notebook.js can't be run here (see the manual check in the PR); pin what it is built from."""
    js = (Path(__file__).resolve().parents[1] / "static" / "notebook.js").read_text()
    nudge = js[js.index("The leaving nudge:") :]

    assert 'addEventListener("mouseleave"' in nudge and "event.clientY <= 0" in nudge and "event.buttons === 0" in nudge
    assert 'addEventListener("visibilitychange"' in nudge and '"hidden"' in nudge
    assert "localStorage" in nudge and "data-key" in nudge
    assert "dialog.open" in nudge and 'event.key === "Escape"' in nudge
    assert "setTimeout" not in js and "setInterval" not in js  # no timers, no polling
    assert ".focus()" not in nudge  # it never takes focus


# --- after "Move to next stage" -------------------------------------------------------------------------------


def test_moving_to_the_next_stage_shows_a_toast_with_a_link_and_does_not_open_the_notebook(session, as_user):
    add_task(session)
    client = as_user(MEMBER)

    response = client.post(
        "/tasks/M06/status", data={"action": "advance", "step": "Drawing"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/tasks/M06"  # no ?log=
    page = client.get(response.headers["location"]).text
    assert 'id="move-toast"' in page and "M06 moved to “Lead review”." in page
    assert 'href="/notebook?task=M06" data-notebook-open' in page and "Write today’s entry" in page
    assert "data-open-url" not in page  # nothing opens by itself
    assert 'id="move-toast"' not in client.get("/tasks/M06").text  # shown once


def test_the_toast_follows_a_move_made_from_home_and_says_finished_on_the_last_stage(session, as_user):
    task = add_task(session)
    task.stage = "Inspect"
    session.add(task)
    session.commit()
    client = as_user(MEMBER)

    response = client.post(
        "/tasks/M06/status", data={"action": "advance", "step": "Inspect", "next": "/"}, follow_redirects=False
    )

    assert response.headers["location"] == "/"
    assert "M06 is finished." in client.get("/").text


def test_a_lead_moving_someone_elses_task_gets_the_words_without_the_notebook_link(session, as_user):
    add_task(session, owner="Riley Sample")
    lead = as_user(LEAD)

    lead.post("/tasks/M06/status", data={"action": "advance", "step": "Drawing"}, follow_redirects=False)

    page = lead.get("/tasks/M06").text
    assert "M06 moved to “Lead review”." in page and "Write today’s entry" not in page.split('id="move-toast"')[1].split("</aside>")[0]


@pytest.mark.parametrize("action, status", [("unclaim", "in_progress"), ("start", "todo")])
def test_other_moves_show_no_toast(session, as_user, action, status):
    add_task(session, status=status)
    client = as_user(MEMBER)

    response = client.post("/tasks/M06/status", data={"action": action}, follow_redirects=False)

    assert response.headers["location"] == "/tasks/M06"
    assert 'id="move-toast"' not in client.get("/tasks/M06").text


def test_a_refused_move_shows_no_toast(session, as_user):
    add_task(session, owner="Riley Sample")
    client = as_user(MEMBER)

    response = client.post("/tasks/M06/status", data={"action": "advance", "step": "Drawing"}, follow_redirects=False)

    assert response.status_code == 403 and 'id="move-toast"' not in client.get("/tasks/M06").text


def test_a_toast_is_not_used_up_by_a_fragment(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    client.post("/tasks/M06/status", data={"action": "advance", "step": "Drawing"}, follow_redirects=False)

    client.get("/notebook/panel", headers=HTMX)

    assert 'id="move-toast"' in client.get("/tasks/M06").text


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
    assert "Cut the panels." in as_user(MEMBER).get("/notebook/panel?tab=earlier").text


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


def test_moving_a_task_on_or_answering_a_notification_writes_nothing_to_the_crush_document(session, as_user, doc):
    import review

    task = add_task(session, "M03", status="needs_review", owner="Riley Sample")
    asked = review.notify(session, task, {"name": "Riley Sample", "email": "riley@example.edu", "role": "member"}, "Look?")
    client = as_user(LEAD)

    client.post("/tasks/M03/status", data={"action": "advance"}, follow_redirects=False)
    client.post(f"/inbox/review/{asked.event.id}/seen", follow_redirects=False)

    assert doc.gets == 0 and stored(session, Decision) == []


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
    assert "Retry" in record_page
    doc.fail_get = None

    retried = client.post(f"/record/answers/{decision.id}/retry", data={"part": "enclosure"}, follow_redirects=False)

    assert retried.status_code == 303 and retried.headers["location"] == f"/record?part=enclosure#answer-{decision.id}"
    assert doc.tab("Enclosure").text().count("Baseplate gasket hole clearance: 0.050 in") == 1
    assert "Retry" not in client.get("/record").text


def test_members_cannot_retry_an_answers_write_and_see_no_button(session, as_user, doc):
    decision = Decision(subsystem="Enclosure", topic="T", decision="A", rationale="", status="confirmed",
                        proposed_by="Lee Lead", confirmed_by="Lee Lead", confirmed_at=utc_now())
    session.add(decision)
    session.commit()
    client = as_user(MEMBER)

    assert client.post(f"/record/answers/{decision.id}/retry").status_code == 403
    assert "Retry" not in client.get("/record").text
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

    response = as_user(LEAD).post(f"/record/answers/{decision.id}/retry", follow_redirects=False)

    assert response.status_code == 303
    assert doc.gets == 0 and stored(session, DriveWrite) == []


# --- an entry's states: Draft, Published, Failed to publish ---------------------------------------


def make_draft(client, **data):
    data = {"task": "M06", "did": "Cut the first panel.", "why": "", **data}
    return client.post("/notebook/entry/save", data=data, headers=HTMX)


def only(session, model=WorkNote):
    (row,) = stored(session, model)
    return row


def test_save_draft_keeps_it_private_and_writes_nothing_to_the_document(session, as_user, doc):
    add_task(session)

    response = make_draft(as_user(MEMBER), why="the latch holes moved")

    assert response.status_code == 200 and "Draft saved." in response.text
    note = only(session)
    assert (note.state, note.did, note.why) == ("draft", "Cut the first panel.", "the latch holes moved")
    assert note.member == "sam@example.edu"
    assert doc.gets == 0 and stored(session, DriveWrite) == []
    # The form comes back holding the draft, so the next save is the same entry.
    assert f'name="entry" value="{note.id}"' in response.text
    assert "Cut the first panel." in response.text


def test_a_draft_shows_in_earlier_with_a_mark_and_the_word(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)
    make_draft(client)

    html = client.get("/notebook/panel?tab=earlier").text

    assert '<span class="status status--todo">Draft</span>' in html
    assert "Only you can see this draft." in html and "Edit draft" in html
    assert "Open in doc" not in html


def test_saving_again_updates_the_same_draft(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)
    make_draft(client)
    note = only(session)

    make_draft(client, entry=str(note.id), did="Cut both panels.")

    assert only(session).did == "Cut both panels." and len(stored(session, WorkNote)) == 1


def test_reopening_today_shows_the_draft_being_written(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)
    make_draft(client, did="Half-written thought")

    html = client.get("/notebook/panel?tab=today").text

    assert "Half-written thought" in html and 'name="entry"' in html


def test_today_defaults_to_the_task_the_member_is_working_on(session, as_user):
    add_task(session, "M06")
    two = add_task(session, "M07", title="Second task", status="todo")
    active(session, two)

    html = as_user(MEMBER).get("/notebook/panel").text

    assert '<option value="M07" selected>' in html


def test_publishing_a_draft_makes_it_published_and_read_only(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)
    make_draft(client)
    note = only(session)

    response = client.post(
        "/notebook/entry/publish", data={"entry": str(note.id), "task": "M06", "did": "Cut both panels.", "why": "rev C"},
        headers=HTMX,
    )

    assert "Published. Written to the Enclosure tab" in response.text
    note = only(session)
    assert (note.state, note.did) == ("published", "Cut both panels.")
    assert doc.lines("Enclosure")[-1] == "Sam Example · M06: Cut both panels. Why: rev C."
    # A published entry can't be edited: saving over it is refused and changes nothing.
    again = client.post(
        "/notebook/entry/save", data={"entry": str(note.id), "task": "M06", "did": "Changed my mind"},
        headers=HTMX,
    )
    assert again.status_code == 404 and "isn't there any more" in again.text.replace("&#39;", "'")
    assert only(session).did == "Cut both panels."
    assert 'name="did"' in again.text and "Changed my mind" in again.text  # their words aren't lost
    # ...and the page doesn't offer to edit it.
    assert "Edit draft" not in client.get("/notebook/panel?tab=earlier").text


def test_publishing_the_same_entry_twice_writes_it_once(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)
    make_draft(client)
    note = only(session)
    data = {"entry": str(note.id), "task": "M06", "did": "Cut the first panel."}

    client.post("/notebook/entry/publish", data=data, headers=HTMX)
    client.post("/notebook/entry/publish", data=data, headers=HTMX)

    assert doc.tab("Enclosure").text().count("Cut the first panel.") == 1
    assert len(stored(session, WorkNote)) == 1 and len(stored(session, DriveWrite)) == 1


def test_publishing_needs_something_written(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)
    make_draft(client, did="Started this")
    note = only(session)

    response = client.post(
        "/notebook/entry/publish", data={"entry": str(note.id), "task": "M06", "did": " "}, headers=HTMX
    )

    assert response.status_code == 400 and "Write what you did before publishing" in response.text
    assert only(session).state == "draft" and doc.gets == 0


def test_discarding_a_draft_removes_it(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)
    make_draft(client)
    note = only(session)

    response = client.post("/notebook/entry/discard", data={"entry": str(note.id), "task": "M06"}, headers=HTMX)

    assert "Draft discarded." in response.text and stored(session, WorkNote) == []


def test_a_published_entry_cannot_be_discarded(session, as_user, doc):
    task = add_task(session)
    note = add_note(session, task)

    response = as_user(MEMBER).post(
        "/notebook/entry/discard", data={"entry": str(note.id), "task": "M06"}, headers=HTMX
    )

    assert response.status_code == 404 and len(stored(session, WorkNote)) == 1


def test_a_draft_is_not_written_up_for_the_nudge(session, as_user):
    task = add_task(session)
    active(session, task)
    make_draft(as_user(MEMBER))

    assert NUDGE in as_user(MEMBER).get("/").text  # only a published entry counts


def test_a_published_entry_whose_write_worked_is_published_and_one_whose_write_failed_says_so(
    session, as_user, doc
):
    task = add_task(session)
    good, bad = add_note(session, task, did="Written one."), add_note(session, task, did="Unwritten one.")
    drive_write.write_note(session, good)
    doc.fail_get = http_error(503, "x")
    drive_write.write_note(session, bad)

    html = as_user(MEMBER).get("/notebook/panel?tab=earlier").text

    assert html.count("status--done") == 1 and html.count("status--review") == 1
    assert ">Published</span>" in html and ">Failed to publish</span>" in html
    assert html.count('action="/notebook/entries/') == 1  # only the failed one has Retry


def test_existing_notes_without_a_draft_state_are_published(session, as_user, doc):
    task = add_task(session)
    note = add_note(session, task)  # the way every note was made before the Notebook

    assert note.state == "published"
    drive_write.write_note(session, note)
    assert ">Published</span>" in as_user(MEMBER).get("/notebook/panel?tab=earlier").text


# --- adding to an earlier entry --------------------------------------------------------------------


def test_a_follow_up_refers_to_the_earlier_entry_and_leaves_it_alone(session, as_user, doc):
    task = add_task(session)
    first = add_note(session, task, did="Cut the panel to 3 mm.", when=utc_now() - timedelta(days=2))
    drive_write.write_note(session, first)
    client = as_user(MEMBER)

    form = client.get(f"/notebook/panel?tab=today&refers={first.id}").text
    assert f'name="refers" value="{first.id}"' in form and "The earlier entry stays as it was." in form
    assert "Cut the panel to 3 mm" in form

    client.post(
        "/notebook/entry/publish",
        data={"task": "M06", "did": "Correction: it was 4 mm.", "refers": str(first.id)},
        headers=HTMX,
    )

    second = max(stored(session, WorkNote), key=lambda n: n.id)
    assert second.refers_to_id == first.id and second.id != first.id
    assert stored(session, WorkNote)[0].did == "Cut the panel to 3 mm."  # never edited
    line = doc.lines("Enclosure")[-1]
    assert line.startswith("Sam Example · M06: Correction: it was 4 mm.") and "Adds to their entry of" in line
    earlier = client.get("/notebook/panel?tab=earlier").text
    assert "Adds to your" in earlier and "Cut the panel to 3 mm" in earlier


def test_one_cannot_follow_up_someone_elses_entry(session, as_user, doc):
    task = add_task(session)
    theirs = add_note(session, task, member=OTHER, did="Riley's private-ish note.")
    client = as_user(MEMBER)

    form = client.get(f"/notebook/panel?tab=today&refers={theirs.id}").text
    client.post(
        "/notebook/entry/publish", data={"task": "M06", "did": "Mine.", "refers": str(theirs.id)}, headers=HTMX
    )

    assert "Riley" not in form and 'name="refers"' not in form
    mine = next(n for n in stored(session, WorkNote) if n.did == "Mine.")
    assert mine.refers_to_id is None


# --- Earlier: 20 at a time, filter by task ------------------------------------------------------------


def test_earlier_shows_twenty_at_a_time_and_more_on_request(session, as_user):
    task = add_task(session)
    for number in range(25):
        add_note(session, task, did=f"Entry number {number:02d}.", when=utc_now() - timedelta(minutes=number))
    client = as_user(MEMBER)

    first = client.get("/notebook/panel?tab=earlier").text
    more = client.get("/notebook/panel?tab=earlier&page=2").text

    assert first.count('class="entry"') == 20 and "Entry number 19." in first and "Entry number 20." not in first
    assert "Show 20 more" in first
    assert more.count('class="entry"') == 25 and "Show 20 more" not in more


def test_earlier_filters_by_task_and_only_offers_the_members_own_tasks(session, as_user):
    one, two = add_task(session, "M06"), add_task(session, "M07", title="Second")
    other = add_task(session, "M09", owner="Riley Sample", title="Riley's")
    add_note(session, one, did="Work on six.")
    add_note(session, two, did="Work on seven.")
    add_note(session, other, member=OTHER, did="Work on nine.")
    client = as_user(MEMBER)

    html = client.get("/notebook/panel?tab=earlier&filter=M07").text

    assert "Work on seven." in html and "Work on six." not in html
    assert '<option value="M06">' in html and '<option value="M09">' not in html
    assert "No entries on M99 yet." in client.get("/notebook/panel?tab=earlier&filter=M99").text


# --- no one else sees a draft -------------------------------------------------------------------------


def test_no_other_member_or_lead_sees_a_draft_anywhere(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)
    make_draft(client, did="SECRET-DRAFT-MARKER cut the lid", why="SECRET-WHY-MARKER")
    note = only(session)

    for who in (OTHER, LEAD):
        viewer = as_user(who)
        for path in (
            "/", "/tasks", "/tasks/M06", "/record?notes=1", "/record?notes=1&part=thrusters",
            "/notebook", "/notebook?tab=earlier", "/notebook?tab=private", "/notebook/panel?tab=earlier",
            f"/notebook/panel?tab=today&entry={note.id}", f"/notebook?entry={note.id}", "/timeline", "/inbox",
        ):
            response = viewer.get(path)
            assert "SECRET-DRAFT-MARKER" not in response.text and "SECRET-WHY-MARKER" not in response.text, (who["name"], path)
        # They can't act on it either.
        data = {"entry": str(note.id), "task": "M06", "did": "hijacked"}
        for action in ("save", "publish", "discard", "tidy", "specs"):
            assert viewer.post(f"/notebook/entry/{action}", data=data, headers=HTMX).status_code in (400, 404), action
        assert viewer.post(f"/notebook/entries/{note.id}/retry", headers=HTMX).status_code == 404

    assert only(session).did.startswith("SECRET-DRAFT") and only(session).state == "draft"
    assert doc.gets == 0 and stored(session, DriveWrite) == []


def test_a_draft_does_not_count_for_the_record_the_history_or_the_has_notes_toggle(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    make_draft(client, did="Draft words")

    assert "work notes" not in client.get("/record").text
    assert "Draft words" not in client.get("/tasks/M06").text
    assert notebook.has_notes(session, ("Enclosure",)) is False


def test_the_model_is_not_called_by_saving_a_draft_or_opening_pages(session, as_user, monkeypatch):
    add_task(session)
    model = use_model(monkeypatch)
    client = as_user(MEMBER)

    make_draft(client)
    for path in ("/notebook", "/notebook/panel", "/notebook/panel?tab=earlier", "/notebook/panel?tab=private"):
        client.get(path)

    assert model.requests == []
