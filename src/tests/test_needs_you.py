"""A lead's "Needs you" list on Home: work to check, answers to settle, new tasks.

Each card kind, who can act on it (leads only, checked on the server), the count on the
Home link, and a member's side: "Ask a lead to settle this" and "Suggest a task".
Made-up people and tasks only.
"""

import json
from datetime import timedelta

import fake_openai as fake
import pytest
from fastapi.testclient import TestClient
from index_helpers import add_file
from sqlmodel import select

import auth
import home
import inbox
import llm
import main
import review
import shell
from models import (
    AnswerRequest,
    CoachMessage,
    Decision,
    DecisionTask,
    LlmCall,
    Task,
    TaskEvent,
    TaskSuggestion,
    utc_now,
)

MEMBER = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}
LEAD = {"name": "Lee Lead", "email": "lee@example.edu", "role": "lead"}
DOC = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"
OLD = "https://docs.google.com/document/d/9ZyXwVuTsRqPoNmL/edit"
CONFLICT = (
    f"**Likely current: 0.050 in** from [Crush Documentation]({DOC}). The "
    f"[Seal Calculations]({OLD}) say 0.030 in. A lead should confirm.\n\n"
    "Next step: ask a lead to confirm the clearance."
)
NOTHING = "Nothing in the Drive says which lubricant.\n\nNext step: ask a lead."
SOURCES = [
    {"title": "Crush Documentation", "link": DOC, "modified": "2026-03-30"},
    {"title": "Seal Calculations", "link": OLD, "modified": "2026-02-10"},
]


@pytest.fixture
def as_user(monkeypatch):
    """Sign in as a user for both the page dependency and the shell's own lookup."""
    clients = []

    def make(user):
        monkeypatch.setattr(auth, "session_user", lambda request: user)
        main.app.dependency_overrides[auth.require_user] = lambda: user
        c = TestClient(main.app)
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    main.app.dependency_overrides.clear()


def add_task(session, code, status="in_progress", owner="Alex Example", **kw):
    task = Task(
        code=code,
        title=kw.pop("title", f"Task {code}"),
        subsystem=kw.pop("subsystem", "Enclosure"),
        task_type="Fabricate",
        stage="Lead review" if status == "needs_review" else "Drawing",
        status=status,
        owner=owner,
        **kw,
    )
    session.add(task)
    session.commit()
    return task


def notify(session, task, message="", days_ago=0, entry_id=None, follow_up=False):
    """A member (the task's owner) notifies a lead about the task, `days_ago` days back."""
    owner = {"name": task.owner, "email": "owner@example.edu", "role": "member"}
    done = review.notify(session, task, owner, message, entry_id=entry_id, follow_up=follow_up)
    done.event.created_at = utc_now() - timedelta(days=days_ago)
    session.add(done.event)
    session.commit()
    return done.event


def add_answer(session, task=None, **kw):
    values = {
        "task_id": task.id if task else None,
        "task_code": task.code if task else "M03",
        "subsystem": "Enclosure",
        "question": "Baseplate gasket hole clearance",
        "asked_by": "Sam Example",
        "kind": "conflict",
        "answer_text": CONFLICT,
        "options": [
            {"value": "0.050 in", "source": "Crush Documentation", "link": DOC, "date": "2026-03-30"},
            {"value": "0.030 in", "source": "Seal Calculations", "link": OLD, "date": "2026-02-10"},
        ],
        "sources": SOURCES,
        "suggested": 0,
    }
    values.update(kw)
    request = AnswerRequest(**values)
    session.add(request)
    session.commit()
    return request


def add_suggestion(session, title="Make a drill jig for the lid latch holes", **kw):
    suggestion = TaskSuggestion(title=title, why="Done when: it locates all 8 hole pairs.",
                                suggested_by="Riley Sample", **kw)
    session.add(suggestion)
    session.commit()
    return suggestion


def stored(session, model):
    session.expire_all()
    return list(session.exec(select(model)).all())


# --- the list and the count ---


def test_one_list_holds_all_three_kinds_with_the_count_on_the_inbox_link(session, as_user):
    notify(session, add_task(session, "M01", "needs_review", title="Baseplate gasket drawing"), days_ago=2)
    add_answer(session)
    add_suggestion(session)

    html = as_user(LEAD).get("/inbox").text

    assert "Needs you" in html and "3 waiting" in html
    for kind in ("Work to check", "Answer to settle", "New task"):
        assert kind in html
    # Work first, then answers, then new tasks.
    assert html.index("Work to check") < html.index("Answer to settle") < html.index("New task")
    link = html.split('<a class="sidebar__link" href="/inbox"')[1].split("</a>")[0]
    assert '<span class="sidebar__badge" aria-hidden="true">3</span>' in link
    assert ", 3 waiting on you" in link
    assert "Inbox</span>" in link
    assert "Waiting for your review" not in html


def test_the_count_shows_on_every_page_for_leads_and_not_on_the_tasks_or_home_link(session, as_user):
    notify(session, add_task(session, "M01", "needs_review"))
    add_answer(session)
    lead = as_user(LEAD)

    for path in ("/", "/inbox", "/tasks", "/tasks/M01", "/record"):
        html = lead.get(path).text
        assert '<span class="sidebar__badge" aria-hidden="true">2</span>' in html, path
        assert html.count("sidebar__badge") == 1, path  # only on the Inbox link


def test_an_empty_list_says_nothing_is_waiting(session, as_user):
    html = as_user(LEAD).get("/inbox").text

    assert "Nothing is waiting on you." in html and "0 waiting" in html
    assert "sidebar__badge" not in html


def test_home_no_longer_holds_the_list_even_for_leads(session, as_user):
    notify(session, add_task(session, "M01", "needs_review"))
    add_answer(session)

    html = as_user(LEAD).get("/").text

    assert "Needs you" not in html and "Baseplate gasket hole clearance" not in html
    assert "Accept" not in html and "Seen" not in html


def test_members_get_no_inbox_tab_and_cannot_open_it(session, as_user):
    notify(session, add_task(session, "M01", "needs_review"))
    add_answer(session)
    member = as_user(MEMBER)

    home = member.get("/").text
    page = member.get("/inbox")

    assert 'href="/inbox"' not in home and "sidebar__badge" not in home
    assert page.status_code == 403
    assert "Baseplate gasket hole clearance" not in page.text


def test_signed_out_visitors_are_sent_to_sign_in_and_see_nothing(session, monkeypatch):
    add_answer(session)
    monkeypatch.undo()

    with TestClient(main.app) as visitor:
        home = visitor.get("/").text
        response = visitor.get("/inbox", follow_redirects=False)

    assert response.status_code == 303 and "/login" in response.headers["location"]
    assert 'href="/inbox"' not in home and "Baseplate gasket hole clearance" not in home


def test_a_database_failure_hides_the_count_but_not_the_page(session, as_user, monkeypatch):
    notify(session, add_task(session, "M01", "needs_review"))
    lead = as_user(LEAD)

    class Broken:
        def __enter__(self):
            raise RuntimeError("postgresql://user:secret@host/db is down")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(shell, "Session", lambda engine: Broken())

    response = lead.get("/tasks")

    assert response.status_code == 200
    assert "sidebar__badge" not in response.text


def test_one_kind_failing_to_load_only_costs_that_kind(session, as_user, monkeypatch):
    add_answer(session)
    add_suggestion(session, title="Make a drill jig")

    def broken(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(inbox, "answer_cards", broken)

    response = as_user(LEAD).get("/inbox")

    assert response.status_code == 200
    assert "Couldn&#39;t load the answers to settle right now." in response.text
    assert "Make a drill jig" in response.text and "secret" not in response.text


# --- work to check ---


def test_a_work_card_has_the_member_task_stage_message_how_long_ago_and_links(session, as_user):
    newer = add_task(session, "M01", "needs_review", title="Newer one")
    older = add_task(
        session, "M02", "needs_review", title="Older one", owner="Riley Sample",
        documentation=f"Drawing at {DOC}",
    )
    notify(session, newer, "Please confirm the hole spacing", days_ago=1)
    notify(session, older, "Second look at the lid?", days_ago=4)
    add_task(session, "M03", "in_progress", title="Not waiting")

    html = as_user(LEAD).get("/inbox").text

    assert html.index("Older one") < html.index("Newer one")  # longest wait first
    assert "Not waiting" not in html
    assert "Riley Sample notified a lead on <b>Lead review</b> · 4 days ago" in html
    assert "“Second look at the lid?”" in html and "“Please confirm the hole spacing”" in html
    assert 'href="/tasks/M02">Open task</a>' in html
    assert f'href="{DOC}"' in html  # the task's documentation link
    assert ">Seen</button>" in html and ">Send back</button>" in html
    assert "Accept" not in html and "Anything decided here worth recording?" not in html


def test_a_task_that_only_sits_on_a_review_stage_has_no_card_and_no_count(session, as_user):
    add_task(session, "M01", "needs_review", title="Just sitting there")

    html = as_user(LEAD).get("/inbox").text

    assert "Just sitting there" not in html and "Work to check" not in html
    assert "Nothing is waiting on you." in html and "sidebar__badge" not in html


def test_a_notification_makes_one_card_with_its_message(session, as_user):
    task = add_task(session, "M01", "needs_review", title="Lid drawing")
    notify(session, task, "Please confirm the hole spacing")

    html = as_user(LEAD).get("/inbox").text

    assert html.count("Work to check") == 1 and "1 waiting" in html
    assert "Lid drawing" in html and "Please confirm the hole spacing" in html


def test_a_follow_up_adds_to_the_same_card_not_a_second_one(session, as_user):
    task = add_task(session, "M01", "needs_review", title="Lid drawing")
    notify(session, task, "First message")
    notify(session, task, "Follow-up message", follow_up=True)

    html = as_user(LEAD).get("/inbox").text

    assert html.count("Work to check") == 1 and "1 waiting" in html
    assert "Follow-up message" in html and "Earlier: “First message”" in html and "2 messages" in html


def test_the_card_links_to_a_published_entry_and_names_a_draft_without_a_link(session, as_user):
    from models import WorkNote

    task = add_task(session, "M01", "needs_review", title="Lid drawing")
    other = add_task(session, "M02", "needs_review", title="Plate drawing", subsystem="Thrusters")
    published = WorkNote(member="owner@example.edu", member_name="Alex Example", task_id=task.id,
                         task_code="M01", subsystem="Enclosure", did="Cut it.", original="Cut it.")
    draft = WorkNote(member="owner@example.edu", member_name="Alex Example", task_id=other.id,
                     task_code="M02", subsystem="Thrusters", did="Maybe.", original="Maybe.", state="draft")
    session.add(published)
    session.add(draft)
    session.commit()
    notify(session, task, "Look?", entry_id=published.id)
    notify(session, other, "Look?", entry_id=draft.id)

    html = as_user(LEAD).get("/inbox").text

    assert f'href="/record?part=enclosure&amp;notes=1#note-{published.id}">Open entry</a>' in html
    assert "Entry (still a draft, not shared yet)" in html
    assert "Maybe." not in html  # a draft stays its member's alone


def test_seen_removes_the_card_and_leaves_the_task_where_it_is(session, as_user):
    task = add_task(session, "M01", "needs_review")
    event = notify(session, task, "Look?")

    response = as_user(LEAD).post(f"/inbox/review/{event.id}/seen", follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/inbox"
    session.expire_all()
    assert session.get(Task, task.id).status == "needs_review"
    seen = [e for e in stored(session, TaskEvent) if e.kind == "review_seen"]
    assert [(e.actor, e.task_id) for e in seen] == [("Lee Lead", task.id)]
    html = as_user(LEAD).get("/inbox").text
    assert "Work to check" not in html and "sidebar__badge" not in html


def test_a_new_notification_after_seen_makes_a_new_card(session, as_user):
    task = add_task(session, "M01", "needs_review")
    event = notify(session, task, "First")
    as_user(LEAD).post(f"/inbox/review/{event.id}/seen")

    notify(session, task, "Second")

    html = as_user(LEAD).get("/inbox").text
    assert html.count("Work to check") == 1 and "Second" in html and "First" not in html


def test_send_back_needs_a_reason(session, as_user):
    task = add_task(session, "M01", "needs_review")
    event = notify(session, task, "Look?")

    response = as_user(LEAD).post(f"/inbox/review/{event.id}/send-back", data={"reason": "   "})

    assert response.status_code == 400 and "Say in a line why you are sending it back." in response.text
    session.expire_all()
    assert session.get(Task, task.id).status == "needs_review"
    assert [e.kind for e in stored(session, TaskEvent)].count("send_back") == 0
    assert "Work to check" in as_user(LEAD).get("/inbox").text  # still waiting


def test_send_back_moves_a_task_on_the_review_stage_to_the_working_stage_before_it(session, as_user):
    task = add_task(session, "M01", "needs_review")
    event = notify(session, task, "Look?")

    response = as_user(LEAD).post(
        f"/inbox/review/{event.id}/send-back", data={"reason": "Redo the holes."}, follow_redirects=False
    )

    assert response.status_code == 303
    session.expire_all()
    task = session.get(Task, task.id)
    assert (task.status, task.stage) == ("in_progress", "Drawing")
    (sent,) = [e for e in stored(session, TaskEvent) if e.kind == "send_back"]
    assert (sent.actor, sent.note, sent.detail) == ("Lee Lead", "Redo the holes.", "Drawing")
    assert "Work to check" not in as_user(LEAD).get("/inbox").text


def test_send_back_moves_a_task_that_is_already_past_the_review_stage_back_too(session, as_user):
    task = add_task(session, "M01", "in_progress")
    task.stage = "Shop"
    session.add(task)
    session.commit()
    event = notify(session, task, "Look?")
    stage = review.meta(event)["stage"]
    assert stage == "Shop"

    as_user(LEAD).post(f"/inbox/review/{event.id}/send-back", data={"reason": "Drawing was wrong."})

    session.expire_all()
    assert session.get(Task, task.id).stage == "Drawing"


def test_send_back_leaves_a_task_that_has_not_reached_the_review_stage_where_it_is(session, as_user):
    task = add_task(session, "M01", "in_progress")  # on Drawing; Lead review is next
    event = notify(session, task, "Is this heading the right way?")

    as_user(LEAD).post(f"/inbox/review/{event.id}/send-back", data={"reason": "Talk to me first."})

    session.expire_all()
    task = session.get(Task, task.id)
    assert (task.status, task.stage) == ("in_progress", "Drawing")
    (sent,) = [e for e in stored(session, TaskEvent) if e.kind == "send_back"]
    assert sent.note == "Talk to me first." and sent.detail is None
    assert "Work to check" not in as_user(LEAD).get("/inbox").text


def test_the_member_sees_sent_back_with_the_reason_on_home(session, as_user, monkeypatch):
    task = add_task(session, "M01", "needs_review", owner="Sam Example")
    event = notify(session, task, "Look?")
    as_user(LEAD).post(f"/inbox/review/{event.id}/send-back", data={"reason": "Redo the holes."})
    monkeypatch.setattr(home.auth, "session_user", lambda request: MEMBER)

    html = as_user(MEMBER).get("/").text

    assert "Sent back:" in html and "Redo the holes." in html


def test_a_member_gets_403_on_seen_and_send_back(session, as_user):
    task = add_task(session, "M01", "needs_review")
    event = notify(session, task, "Look?")
    member = as_user(MEMBER)

    seen = member.post(f"/inbox/review/{event.id}/seen")
    back = member.post(f"/inbox/review/{event.id}/send-back", data={"reason": "No."})

    assert seen.status_code == 403 and back.status_code == 403
    session.expire_all()
    assert session.get(Task, task.id).status == "needs_review"
    assert [e.kind for e in stored(session, TaskEvent)] == ["review_requested"]


def test_answering_a_notification_someone_already_answered_is_a_409(session, as_user):
    task = add_task(session, "M01", "needs_review")
    event = notify(session, task, "Look?")
    lead = as_user(LEAD)
    lead.post(f"/inbox/review/{event.id}/seen")

    again = lead.post(f"/inbox/review/{event.id}/send-back", data={"reason": "Too late."})

    assert again.status_code == 409 and "already answered" in again.text
    session.expire_all()
    assert session.get(Task, task.id).status == "needs_review"


def test_a_lead_who_notifies_themselves_gets_a_card_like_any_other(session, as_user):
    task = add_task(session, "M01", "needs_review", owner="Lee Lead")
    review.notify(session, task, LEAD, "Check my work")

    html = as_user(LEAD).get("/inbox").text

    assert "Check my work" in html and "self-approved" not in html and "Accepting" not in html


def test_the_task_page_for_a_lead_points_to_the_inbox_only_when_a_notification_waits(session, as_user):
    task = add_task(session, "M01", "needs_review")
    lead = as_user(LEAD)

    plain = lead.get("/tasks/M01").text
    assert ">Approve" not in plain and "Accept" not in plain
    assert "See it in the Inbox" not in plain

    notify(session, task, "Look?")
    waiting = lead.get("/tasks/M01").text
    assert 'href="/inbox">See it in the Inbox</a>' in waiting and "Look?" in waiting


def test_the_tasks_page_has_no_review_panel(session, as_user):
    add_task(session, "M01", "needs_review", title="Waiting one")
    add_task(session, "M02", "in_progress", title="Busy one")

    html = as_user(LEAD).get("/tasks?show=review").text

    assert 'href="/tasks?show=review" aria-current="true">Needs review</a>' in html
    assert "Waiting one" in html and "Busy one" not in html  # the filter is still a view
    assert "Waiting for review" not in html and "Accept" not in html


# --- answers to settle ---


def test_an_answer_card_has_the_question_task_options_with_sources_and_the_suggestion(
    session, as_user
):
    add_answer(session)

    html = as_user(LEAD).get("/inbox").text

    assert "Baseplate gasket hole clearance" in html
    assert "Asked by Sam Example on <span class=\"code\">M03</span>. The sources disagree." in html
    assert 'class="is-suggested"' in html and "checked" in html
    assert "0.050 in" in html and "0.030 in" in html
    assert "Coach suggests" in html
    assert f'href="{DOC}"' in html and "Crush Documentation</a>, 2026-03-30" in html
    assert "Something else" in html
    assert "Record answer</button>" in html and "Not now</button>" in html


def test_a_card_for_a_question_nothing_answers_has_the_coachs_suggestion_and_a_box(
    session, as_user
):
    add_answer(
        session, kind="none", options=[], suggested=None, question="O-ring lubricant",
        answer_text=NOTHING, suggestion="Ask a lead.",
    )

    html = as_user(LEAD).get("/inbox").text

    assert "Nothing in the Drive answers it." in html
    assert "<b>Coach suggests</b> Ask a lead." in html
    assert 'name="typed"' in html and 'name="choice"' not in html


def test_a_lead_records_one_of_the_options(session, as_user):
    task = add_task(session, "M03")
    request = add_answer(session, task)

    response = as_user(LEAD).post(
        f"/answers/{request.id}/record", data={"choice": "0"}, follow_redirects=False
    )

    assert response.status_code == 303 and response.headers["location"] == "/inbox"
    (decision,) = stored(session, Decision)
    assert decision.status == "confirmed" and decision.subsystem == "Enclosure"
    assert decision.topic == "Baseplate gasket hole clearance" and decision.decision == "0.050 in"
    assert decision.rationale == "From Crush Documentation, 2026-03-30."
    assert decision.sources == [{"title": "Crush Documentation", "link": DOC, "folder": "", "year": None}]
    assert decision.confirmed_by == "Lee Lead"
    assert [link.task_id for link in stored(session, DecisionTask)] == [task.id]
    settled = stored(session, AnswerRequest)[0]
    assert settled.status == "recorded" and settled.decision_id == decision.id
    assert settled.settled_by == "Lee Lead"


def test_a_lead_can_record_another_value_and_typing_wins_over_a_ticked_option(session, as_user):
    request = add_answer(session)

    as_user(LEAD).post(f"/answers/{request.id}/record", data={"choice": "1", "typed": "0.045 in"})

    (decision,) = stored(session, Decision)
    assert decision.decision == "0.045 in" and decision.sources == []


def test_with_no_options_a_lead_types_the_answer(session, as_user):
    request = add_answer(session, kind="none", options=[], suggested=None)

    as_user(LEAD).post(f"/answers/{request.id}/record", data={"typed": "Use the Parker handbook grease."})

    assert stored(session, Decision)[0].decision == "Use the Parker handbook grease."


def test_a_recorded_answer_leaves_the_list_and_lands_in_the_record(session, as_user):
    request = add_answer(session)
    lead = as_user(LEAD)

    lead.post(f"/answers/{request.id}/record", data={"choice": "0"})

    home = lead.get("/inbox").text
    record = lead.get("/record").text
    assert "Baseplate gasket hole clearance" not in home and "Nothing is waiting on you." in home
    assert "Baseplate gasket hole clearance:</b> 0.050 in" in record


def test_recording_a_newer_answer_to_the_same_question_replaces_the_old_one(session, as_user):
    first = add_answer(session)
    second = add_answer(session)
    lead = as_user(LEAD)

    lead.post(f"/answers/{first.id}/record", data={"choice": "1"})
    lead.post(f"/answers/{second.id}/record", data={"choice": "0"})

    old, new = sorted(stored(session, Decision), key=lambda d: d.id)
    assert (old.status, old.decision) == ("superseded", "0.030 in")
    assert (new.status, new.decision) == ("confirmed", "0.050 in")
    assert "Replaces: 0.030 in." in lead.get("/record").text


def test_recording_with_nothing_chosen_says_so_and_keeps_the_question(session, as_user):
    request = add_answer(session, suggested=None)

    response = as_user(LEAD).post(f"/answers/{request.id}/record", data={})

    assert response.status_code == 400
    assert "Choose an answer or type one, then record it." in response.text
    assert "Baseplate gasket hole clearance" in response.text
    assert stored(session, Decision) == []
    assert stored(session, AnswerRequest)[0].status == "open"


def test_a_second_lead_cannot_record_the_same_question_again(session, as_user):
    request = add_answer(session)
    lead = as_user(LEAD)
    lead.post(f"/answers/{request.id}/record", data={"choice": "0"})

    response = lead.post(f"/answers/{request.id}/record", data={"choice": "1"})

    assert response.status_code == 409
    assert "Someone has already recorded an answer" in response.text
    assert len(stored(session, Decision)) == 1


def test_not_now_keeps_the_question_but_moves_it_to_the_end(session, as_user):
    first = add_answer(session, question="First question")
    add_answer(session, question="Second question")
    lead = as_user(LEAD)

    response = lead.post(f"/answers/{first.id}/later", follow_redirects=False)

    assert response.status_code == 303
    html = lead.get("/inbox").text
    assert html.index("Second question") < html.index("First question")
    assert "2 waiting" in html and stored(session, Decision) == []


def test_a_member_cannot_record_or_defer_an_answer(session, as_user):
    request = add_answer(session)
    member = as_user(MEMBER)

    recorded = member.post(f"/answers/{request.id}/record", data={"choice": "0"})
    deferred = member.post(f"/answers/{request.id}/later")

    assert recorded.status_code == 403 and deferred.status_code == 403
    assert stored(session, Decision) == []
    saved = stored(session, AnswerRequest)[0]
    assert saved.status == "open" and saved.deferred_at is None


def test_the_rule_is_checked_in_the_code_as_well_as_the_route(session):
    request = add_answer(session)

    with pytest.raises(inbox.InboxError) as error:
        inbox.record_request(session, MEMBER, request.id, "0")

    assert error.value.status == 403 and stored(session, Decision) == []


def test_a_database_failure_while_recording_says_so_and_changes_nothing(
    session, as_user, monkeypatch
):
    request = add_answer(session)
    lead = as_user(LEAD)

    def broken(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(inbox.decisions, "record_answer", broken)

    response = lead.post(f"/answers/{request.id}/record", data={"choice": "0"})

    assert response.status_code == 503
    assert "Nothing was changed" in response.text and "secret" not in response.text
    assert stored(session, AnswerRequest)[0].status == "open"


# --- a member asks a lead to settle an answer ---


def coach_answer(session, task, content=CONFLICT, sources=SOURCES, question="Gasket hole clearance?"):
    asked = CoachMessage(task_id=task.id, member=MEMBER["email"], role="user", content=question)
    session.add(asked)
    session.commit()
    answer = CoachMessage(
        task_id=task.id, member="Coach", role="assistant", content=content,
        meta=json.dumps({"sources": sources, "notes": []}),
    )
    session.add(answer)
    session.commit()
    return answer


def use_model(monkeypatch, *script):
    model = fake.FakeClient(list(script))
    monkeypatch.setattr(llm, "get_client", lambda: model)
    return model


OPTIONS_JSON = json.dumps(
    {
        "options": [
            {"value": "0.050 in", "source": "Crush Documentation", "link": DOC, "date": "2026-03-30"},
            {"value": "0.030 in", "source": "Seal Calculations", "link": OLD, "date": "2026-02-10"},
        ],
        "suggested": 0,
    }
)


def test_a_conflicting_answer_offers_the_button_and_the_old_propose_link_is_gone(
    session, as_user
):
    task = add_task(session, "M03")
    coach_answer(session, task)

    html = as_user(MEMBER).get("/tasks/M03").text

    assert "Ask a lead to settle this" in html
    assert 'action="/tasks/M03/settle"' in html
    assert "Propose as a decision" not in html


def test_a_fresh_conflicting_answer_arrives_with_the_button(session, as_user, monkeypatch):
    add_task(session, "M03")
    add_file(
        session, "Crush Documentation", chunks=["gasket hole clearance is 0.050 in"],
        drive_id="1AbCdEfGhIjKlMnOp", link=DOC,
    )
    use_model(monkeypatch, fake.reply(CONFLICT))

    response = as_user(MEMBER).post(
        "/tasks/M03/coach", data={"message": "gasket hole clearance?"}, headers={"HX-Request": "true"}
    )

    answer = next(m for m in stored(session, CoachMessage) if m.role == "assistant")
    assert response.status_code == 200
    assert f'<input type="hidden" name="message" value="{answer.id}">' in response.text
    assert "Ask a lead to settle this" in response.text


def test_an_answer_with_no_sources_offers_it_too_but_a_sourced_one_does_not(session, as_user):
    task = add_task(session, "M03")
    coach_answer(session, task, content=NOTHING, sources=[])
    coach_answer(
        session, task, content=f"Use [the notes]({DOC}).\n\nNext step: open it.", sources=SOURCES[:1]
    )

    html = as_user(MEMBER).get("/tasks/M03").text

    assert html.count("Ask a lead to settle this") == 1


def test_pressing_it_makes_an_answer_to_settle_with_options_from_one_model_call(
    session, as_user, monkeypatch
):
    task = add_task(session, "M03", subsystem="Seals/Waterproofing")
    answer = coach_answer(session, task)
    model = use_model(monkeypatch, fake.reply(OPTIONS_JSON))

    response = as_user(MEMBER).post(
        "/tasks/M03/settle", data={"message": str(answer.id)}, follow_redirects=False
    )

    assert response.status_code == 303
    assert len(model.requests) == 1
    (request,) = stored(session, AnswerRequest)
    assert request.status == "open" and request.kind == "conflict"
    assert request.question == "Gasket hole clearance?" and request.task_code == "M03"
    assert request.subsystem == "Seals/Waterproofing" and request.asked_by == "Sam Example"
    assert [o["value"] for o in request.options] == ["0.050 in", "0.030 in"]
    assert request.suggested == 0 and request.sources == SOURCES
    assert [c.status for c in stored(session, LlmCall)] == ["ok"]
    # The member sees it was sent; a lead sees the card.
    assert "Sent to the leads." in as_user(MEMBER).get("/tasks/M03").text
    assert "Gasket hole clearance?" in as_user(LEAD).get("/inbox").text


def test_if_the_model_fails_the_request_is_still_made_with_the_answer_to_read(
    session, as_user, monkeypatch
):
    task = add_task(session, "M03")
    answer = coach_answer(session, task)
    use_model(monkeypatch, fake.timed_out())

    response = as_user(MEMBER).post("/tasks/M03/settle", data={"message": str(answer.id)})

    assert response.status_code == 200
    (request,) = stored(session, AnswerRequest)
    assert request.options == [] and request.answer_text == CONFLICT
    assert [c.status for c in stored(session, LlmCall)] == ["timeout"]
    html = as_user(LEAD).get("/inbox").text
    assert "Likely current:" in html and 'name="typed"' in html  # read it, type the value


def test_an_answer_nothing_answers_needs_no_model_call(session, as_user, monkeypatch):
    task = add_task(session, "M03")
    answer = coach_answer(session, task, content=NOTHING, sources=[], question="Which lubricant?")
    model = use_model(monkeypatch)

    as_user(MEMBER).post("/tasks/M03/settle", data={"message": str(answer.id)})

    assert model.requests == []
    (request,) = stored(session, AnswerRequest)
    assert request.kind == "none" and request.options == [] and request.suggestion == "Ask a lead."
    assert "<b>Coach suggests</b> Ask a lead." in as_user(LEAD).get("/inbox").text


def test_asking_twice_about_one_answer_makes_one_request(session, as_user, monkeypatch):
    task = add_task(session, "M03")
    answer = coach_answer(session, task)
    model = use_model(monkeypatch, fake.reply(OPTIONS_JSON))
    member = as_user(MEMBER)

    member.post("/tasks/M03/settle", data={"message": str(answer.id)})
    member.post("/tasks/M03/settle", data={"message": str(answer.id)})

    assert len(stored(session, AnswerRequest)) == 1 and len(model.requests) == 1


def test_only_a_coach_answer_from_this_task_can_be_handed_over(session, as_user, monkeypatch):
    task = add_task(session, "M03")
    other = add_task(session, "M04")
    theirs = coach_answer(session, other)
    use_model(monkeypatch)

    response = as_user(MEMBER).post("/tasks/M03/settle", data={"message": str(theirs.id)})

    assert "That answer isn&#39;t in this task&#39;s conversation." in response.text
    assert stored(session, AnswerRequest) == [] and task.id != other.id


def test_signed_out_visitors_cannot_ask_a_lead(session, monkeypatch):
    task = add_task(session, "M03")
    answer = coach_answer(session, task)
    monkeypatch.undo()

    with TestClient(main.app) as visitor:
        response = visitor.post(
            "/tasks/M03/settle", data={"message": str(answer.id)}, follow_redirects=False
        )

    assert response.status_code == 303 and "/login" in response.headers["location"]
    assert stored(session, AnswerRequest) == []


# --- new tasks ---


def test_a_member_suggests_a_task_with_a_title_and_why(session, as_user):
    member = as_user(MEMBER)

    response = member.post(
        "/task-suggestions",
        data={"title": "Make a drill jig", "why": "Done when: it locates 8 pairs."},
        follow_redirects=False,
    )

    assert response.status_code == 303 and response.headers["location"].startswith("/tasks")
    (suggestion,) = stored(session, TaskSuggestion)
    assert suggestion.title == "Make a drill jig" and suggestion.status == "open"
    assert suggestion.suggested_by == "Sam Example"
    page = member.get("/tasks?suggested=1").text
    assert "Sent to the leads." in page and "Suggest a task" in page


def test_a_suggestion_needs_a_title(session, as_user):
    response = as_user(MEMBER).post("/task-suggestions", data={"title": "  ", "why": "Because"})

    assert response.status_code == 400
    assert "Give the task a title." in response.text and "Because" in response.text
    assert stored(session, TaskSuggestion) == []


def test_a_new_task_card_has_who_suggested_it_where_it_fits_and_three_buttons(session, as_user):
    suggestion = add_suggestion(session)

    html = as_user(LEAD).get("/inbox").text

    assert "Make a drill jig for the lid latch holes" in html
    assert "Suggested by Riley Sample · Enclosure · Fabricate." in html  # guessed from the words
    assert "Done when: it locates all 8 hole pairs." in html
    assert "Add task</button>" in html and "Dismiss</button>" in html
    assert f'href="/inbox?edit={suggestion.id}#new-{suggestion.id}"' in html  # Edit


def test_a_lead_adds_the_task_and_it_starts_at_the_first_step(session, as_user):
    add_task(session, "M05")
    suggestion = add_suggestion(session, title="Make a lid latch jig")

    response = as_user(LEAD).post(
        f"/task-suggestions/{suggestion.id}/add",
        data={"title": "Make a lid latch jig", "subsystem": "Enclosure", "task_type": "Fabricate",
              "why": "Done when: it locates 8 pairs."},
        follow_redirects=False,
    )

    assert response.status_code == 303
    new = session.exec(select(Task).where(Task.code == "M06")).one()
    assert (new.title, new.subsystem, new.task_type) == ("Make a lid latch jig", "Enclosure", "Fabricate")
    assert (new.status, new.stage, new.owner) == ("todo", "Drawing", None)
    assert new.description == "Done when: it locates 8 pairs."
    done = stored(session, TaskSuggestion)[0]
    assert done.status == "added" and done.task_code == "M06" and done.resolved_by == "Lee Lead"


def test_editing_shows_the_fields_and_what_the_lead_changes_is_what_is_added(session, as_user):
    suggestion = add_suggestion(session, title="Mount the pressure sensor")
    lead = as_user(LEAD)

    page = lead.get(f"/inbox?edit={suggestion.id}").text
    lead.post(
        f"/task-suggestions/{suggestion.id}/add",
        data={"title": "Mount the leak sensor", "subsystem": "Electronics Mounts",
              "task_type": "Design", "why": "Needs a bracket."},
    )

    assert 'name="title"' in page and "<select" in page and "Cancel" in page
    new = session.exec(select(Task)).one()
    assert new.code == "E01" and new.title == "Mount the leak sensor" and new.task_type == "Design"
    assert new.stage == "Requirements"


def test_a_suggestion_whose_subsystem_cannot_be_guessed_opens_for_editing(session, as_user):
    add_suggestion(session, title="Order more stuff")

    html = as_user(LEAD).get("/inbox").text

    assert "Choose a subsystem" in html and "<select" in html


def test_adding_without_a_subsystem_says_to_choose_one(session, as_user):
    suggestion = add_suggestion(session, title="Order more stuff")

    response = as_user(LEAD).post(
        f"/task-suggestions/{suggestion.id}/add",
        data={"title": "Order more stuff", "subsystem": "", "task_type": "Fabricate"},
    )

    assert response.status_code == 400
    assert "Choose a subsystem for the task" in response.text
    assert stored(session, Task) == [] and stored(session, TaskSuggestion)[0].status == "open"


def test_a_lead_dismisses_a_suggestion(session, as_user):
    suggestion = add_suggestion(session)
    lead = as_user(LEAD)

    response = lead.post(f"/task-suggestions/{suggestion.id}/dismiss", follow_redirects=False)

    assert response.status_code == 303
    assert stored(session, TaskSuggestion)[0].status == "dismissed"
    assert stored(session, Task) == []
    assert "Nothing is waiting on you." in lead.get("/inbox").text


def test_a_member_cannot_add_or_dismiss_a_suggestion(session, as_user):
    suggestion = add_suggestion(session)
    member = as_user(MEMBER)

    added = member.post(
        f"/task-suggestions/{suggestion.id}/add",
        data={"title": "x", "subsystem": "Enclosure", "task_type": "Fabricate"},
    )
    dismissed = member.post(f"/task-suggestions/{suggestion.id}/dismiss")

    assert added.status_code == 403 and dismissed.status_code == 403
    assert stored(session, Task) == [] and stored(session, TaskSuggestion)[0].status == "open"


def test_a_suggestion_can_only_be_dealt_with_once(session, as_user):
    suggestion = add_suggestion(session)
    lead = as_user(LEAD)
    lead.post(f"/task-suggestions/{suggestion.id}/dismiss")

    response = lead.post(f"/task-suggestions/{suggestion.id}/dismiss")

    assert response.status_code == 409
    assert "already dealt with this suggestion" in response.text


def test_code_prefixes_follow_the_subsystem(session):
    add_task(session, "T02")

    assert inbox.next_code(session, "Torpedo") == "T03"
    assert inbox.next_code(session, "Gripper") == "G01"
    assert inbox.next_code(session, "Enclosure") == "M01"
