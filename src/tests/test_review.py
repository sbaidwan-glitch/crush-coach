"""Notifying a lead (`review.py`, the Notebook's "Notify team lead for review"): what it
records, the limits, what it never does, and the rules for the card it makes.

Leads are not gates: a notification moves nothing and nothing waits on it. Made-up people
and tasks only."""

from datetime import UTC, datetime, timedelta

import pytest
from notebook_helpers import HTMX, LEAD, MEMBER, OTHER, add_note, add_task, stored

import localtime
import notebook
import review
import steps
import taskpage
from models import DriveWrite, TaskEvent, WorkNote, utc_now

HTMX_FORM = {"task": "M06"}


def notify(client, **data):
    return client.post("/notebook/entry/notify", data={**HTMX_FORM, **data}, headers=HTMX)


def kinds(session, kind):
    return [e for e in stored(session, TaskEvent) if e.kind == kind]


# --- the button and the box ------------------------------------------------------------


def test_the_entry_form_has_a_blue_notify_button_and_a_message_box_beside_publish(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/notebook/panel?task=M06", headers=HTMX).text

    assert 'class="btn btn--notify"' in html and "Notify team lead for review</button>" in html
    assert 'formaction="/notebook/entry/notify"' in html and 'hx-post="/notebook/entry/notify"' in html
    assert 'name="message"' in html and 'maxlength="280"' in html
    assert "Please confirm the hole spacing" in html
    # Publish is still its own button, and the words say they are independent.
    assert "Publish to Crush doc</button>" in html
    assert "publishing doesn’t notify anyone" in html
    assert html.index("Publish to Crush doc</button>") < html.index("Notify team lead for review</button>")


def test_the_button_is_in_the_page_version_too(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/notebook?task=M06").text

    assert "Notify team lead for review</button>" in html and 'name="message"' in html


def test_no_task_no_button(session, as_user):
    html = as_user(MEMBER).get("/notebook/panel", headers=HTMX).text  # owns nothing

    assert "Notify team lead for review" not in html


# --- pressing it -----------------------------------------------------------------------


def test_it_records_the_message_and_the_stage_says_so_and_does_not_move_or_publish(session, as_user, doc):
    task = add_task(session, status="needs_review")
    task.stage = "Lead review"
    session.add(task)
    session.commit()

    response = notify(as_user(MEMBER), message="  Please confirm the\nhole spacing ", did="half a thought")

    assert response.status_code == 200
    assert "Your team lead can see this in their inbox." in response.text
    assert f"Sent at {localtime.clock(stored(session, TaskEvent)[0].created_at)}" in response.text
    (event,) = kinds(session, "review_requested")
    assert event.actor == "Sam Example" and event.detail == "Please confirm the hole spacing"
    assert review.meta(event) == {"stage": "Lead review", "entry": None}
    assert (event.from_status, event.to_status) == ("needs_review", "needs_review")
    session.expire_all()
    assert (task.status, task.stage) == ("needs_review", "Lead review")  # nothing moved
    assert stored(session, WorkNote) == [] and stored(session, DriveWrite) == []  # nothing saved or published
    assert doc.gets == 0
    assert "half a thought" in response.text  # what they typed is still there


def test_the_message_is_optional(session, as_user):
    add_task(session)

    notify(as_user(MEMBER))

    (event,) = kinds(session, "review_requested")
    assert event.detail is None


def test_a_message_over_280_characters_is_refused_and_keeps_the_text(session, as_user):
    add_task(session)

    response = notify(as_user(MEMBER), message="x" * 281)

    assert response.status_code == 400 and "280 characters" in response.text
    assert kinds(session, "review_requested") == []


def test_a_message_of_exactly_280_is_fine(session, as_user):
    add_task(session)

    assert notify(as_user(MEMBER), message="x" * 280).status_code == 200


def test_the_second_press_adds_nothing_and_says_already_sent_with_a_follow_up_option(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    notify(client, message="First")
    (first,) = kinds(session, "review_requested")

    again = notify(client, message="Second")

    assert len(kinds(session, "review_requested")) == 1
    assert f"Already sent at {localtime.clock(first.created_at)}." in again.text
    assert 'name="follow_up" value="1"' in again.text and "Send a follow-up message" in again.text
    assert "Second" in again.text  # their words are kept for the follow-up


def test_a_follow_up_adds_a_new_event(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    notify(client, message="First")

    response = notify(client, message="Any news?", follow_up="1")

    assert [e.detail for e in kinds(session, "review_requested")] == ["First", "Any news?"]
    assert "Your team lead can see this in their inbox." in response.text


def test_at_most_three_a_day_per_task_checked_on_the_server(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    notify(client, message="1")
    notify(client, message="2", follow_up="1")
    notify(client, message="3", follow_up="1")

    fourth = notify(client, message="4", follow_up="1")
    plain = notify(client, message="5")  # not a follow-up either: still can't go over

    assert fourth.status_code == 429 and "3 times today" in fourth.text
    assert plain.status_code == 200 and "Already sent at" in plain.text
    assert [e.detail for e in kinds(session, "review_requested")] == ["1", "2", "3"]
    page = client.get("/notebook/panel?task=M06", headers=HTMX).text
    assert "disabled>Notify team lead for review" in page.replace("\n", "")


def test_the_limit_is_per_task_and_per_day(session, as_user):
    task = add_task(session)
    other = add_task(session, "M07", title="Other task")
    client = as_user(MEMBER)
    for text in ("1", "2", "3"):
        notify(client, message=text, follow_up="1")
    yesterday = utc_now() - timedelta(days=1, hours=1)
    for event in kinds(session, "review_requested"):
        event.created_at = yesterday
        session.add(event)
    session.commit()
    answer = TaskEvent(task_id=task.id, actor="Lee Lead", from_status="in_progress", to_status="in_progress",
                       kind="review_seen")
    session.add(answer)
    session.commit()

    assert notify(client, message="today").status_code == 200  # a new day
    assert notify(client, task="M07", message="other").status_code == 200  # another task
    assert review.sent_today(session, task.id) == 1 and review.sent_today(session, other.id) == 1


def test_someone_who_does_not_own_the_task_cannot_notify_about_it(session, as_user):
    add_task(session, owner="Riley Sample")

    response = notify(as_user(MEMBER), message="Look")

    assert response.status_code == 403
    assert kinds(session, "review_requested") == []


def test_a_lead_can_notify_about_any_task_even_their_own(session, as_user):
    add_task(session, owner="Lee Lead")

    response = notify(as_user(LEAD), message="Check my work")

    assert response.status_code == 200 and len(kinds(session, "review_requested")) == 1


def test_it_works_without_javascript_and_keeps_the_page(session, as_user):
    add_task(session)

    response = as_user(MEMBER).post(
        "/notebook/entry/notify", data={**HTMX_FORM, "message": "Look", "did": "typed"}, follow_redirects=False
    )

    assert response.status_code == 200  # the page itself, not a redirect that would drop the text
    assert "typed" in response.text and "Your team lead can see this in their inbox." in response.text


def test_an_unknown_task_is_refused_plainly(session, as_user):
    add_task(session)

    response = notify(as_user(MEMBER), task="", message="Look")

    assert response.status_code == 400 and "Choose which task" in response.text


def test_a_database_failure_says_so_and_keeps_the_form(session, as_user, monkeypatch):
    add_task(session)

    def boom(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(review, "notify", boom)

    response = notify(as_user(MEMBER), message="Look", did="typed words")

    assert response.status_code == 503 and "secret" not in response.text
    assert "typed words" in response.text


# --- independent of publishing ----------------------------------------------------------


def test_publishing_does_not_notify_and_notifying_does_not_publish(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)

    client.post("/notebook/entry/publish", data={**HTMX_FORM, "did": "Cut it", "message": "Look"}, headers=HTMX)

    assert kinds(session, "review_requested") == []
    assert len(stored(session, WorkNote)) == 1


def test_it_links_a_saved_draft_a_followed_up_entry_or_todays_published_entry_else_only_the_task(session, as_user):
    task = add_task(session)
    client = as_user(MEMBER)

    notify(client, message="no entry")
    assert review.meta(kinds(session, "review_requested")[-1])["entry"] is None

    draft = add_note(session, task, state="draft")
    notify(client, message="draft", entry=str(draft.id), follow_up="1")
    assert review.meta(kinds(session, "review_requested")[-1])["entry"] == draft.id

    published = add_note(session, task, did="Published one.")
    notify(client, message="published", follow_up="1")  # no draft in the form: today's published entry
    assert review.meta(kinds(session, "review_requested")[-1])["entry"] == published.id


def test_someone_elses_draft_is_never_linked(session, as_user):
    task = add_task(session)
    theirs = add_note(session, task, member=OTHER, state="draft")

    notify(as_user(MEMBER), message="Look", entry=str(theirs.id))

    assert review.meta(kinds(session, "review_requested")[-1])["entry"] is None


# --- the pure rules ----------------------------------------------------------------------


def test_clock_reads_like_a_person_says_it(monkeypatch):
    monkeypatch.setenv("COACH_TIMEZONE", "America/Los_Angeles")

    def stored_at(*args):  # how created_at is stored: naive UTC
        return datetime(*args, tzinfo=UTC).replace(tzinfo=None)

    assert localtime.clock(stored_at(2026, 7, 1, 23, 10)) == "4:10 pm"  # PDT, UTC-7
    assert localtime.clock(stored_at(2026, 11, 2, 0, 5)) == "4:05 pm"  # PST, UTC-8
    assert localtime.clock(stored_at(2026, 7, 1, 7, 0)) == "12:00 am"
    assert localtime.clock(stored_at(2026, 7, 1, 19, 0)) == "12:00 pm"


@pytest.mark.parametrize(
    ("status", "stage", "noted", "expected"),
    [
        ("needs_review", "Lead review", "Lead review", "Drawing"),  # still on it
        ("in_progress", "Shop", "Lead review", "Drawing"),  # past it
        ("done", "Inspect", "Lead review", "Drawing"),  # finished
        ("in_progress", "Drawing", "Drawing", None),  # asked before reaching it
        ("in_progress", "Drawing", "Lead review", None),  # notified on it, then sent back by hand
        ("in_progress", "Shop", "Shop", "Drawing"),  # notified later: the review before it
    ],
)
def test_send_back_target(status, stage, noted, expected):
    from models import Task

    task = Task(code="M01", title="t", subsystem="Enclosure", task_type="Fabricate", status=status, stage=stage)

    assert review.send_back_target(task, noted) == expected


def test_send_back_target_with_a_task_that_has_no_review_stage_stays_put():
    from models import Task

    task = Task(code="M01", title="t", subsystem="Enclosure", task_type="Fabricate", status="in_progress",
                stage="Alpha", steps=steps.as_json([steps.Step("Alpha"), steps.Step("Beta")]))

    assert review.send_back_target(task, "Alpha") is None


# --- who is waiting -------------------------------------------------------------------------


def test_unanswered_means_no_lead_answer_after_it(session):
    task = add_task(session)
    first = review.notify(session, task, MEMBER, "a").event
    assert [e.id for e in review.open_requests(session, task.id)] == [first.id]

    review.mark_seen(session, LEAD, first.id)
    assert review.open_requests(session, task.id) == [] and review.count(session) == 0

    second = review.notify(session, task, MEMBER, "b").event
    follow = review.notify(session, task, MEMBER, "c", follow_up=True).event
    assert [e.id for e in review.open_requests(session, task.id)] == [second.id, follow.id]
    assert review.count(session) == 1  # one task, one card


def test_an_old_approval_answers_a_notification_too(session):
    task = add_task(session)
    review.notify(session, task, MEMBER, "a")
    session.add(TaskEvent(task_id=task.id, actor="Lee Lead", from_status="needs_review",
                          to_status="in_progress", kind="approve", detail="Lead review"))
    session.commit()

    assert review.count(session) == 0


def test_a_removed_task_leaves_no_card(session):
    task = add_task(session)
    review.notify(session, task, MEMBER, "a")
    session.delete(task)
    session.commit()

    assert review.cards(session) == [] and review.count(session) == 0


def test_a_lead_pressing_seen_is_not_working_on_the_task_for_the_nudge(session):
    task = add_task(session, owner="Riley Sample")
    asked = review.notify(session, task, {"name": "Riley Sample", "email": "riley@example.edu", "role": "member"}, "a")
    review.mark_seen(session, LEAD, asked.event.id)

    assert notebook.active_task_today(session, LEAD) is None


def test_seen_and_send_back_are_for_leads_only(session):
    task = add_task(session)
    asked = review.notify(session, task, MEMBER, "a")

    for call in (lambda: review.mark_seen(session, MEMBER, asked.event.id),
                 lambda: review.send_back(session, MEMBER, asked.event.id, "no")):
        with pytest.raises(review.ReviewError) as caught:
            call()
        assert caught.value.status == 403


def test_send_back_needs_a_reason_and_cuts_it_short(session):
    task = add_task(session)
    asked = review.notify(session, task, MEMBER, "a")

    with pytest.raises(review.ReviewError):
        review.send_back(session, LEAD, asked.event.id, "  ")
    with pytest.raises(review.ReviewError):
        review.send_back(session, LEAD, asked.event.id, "x" * 201)
    assert review.count(session) == 1  # nothing was answered


def test_the_sent_back_note_stays_until_the_member_moves_on(session):
    task = add_task(session)
    asked = review.notify(session, task, MEMBER, "a")
    review.send_back(session, LEAD, asked.event.id, "Redo it")

    assert review.sent_back_notice(session, task) == {"reason": "Redo it", "by": "Lee Lead", "stage": ""}
    session.add(TaskEvent(task_id=task.id, actor="Sam Example", from_status="in_progress",
                          to_status="in_progress", kind="advance", detail="Drawing"))
    session.commit()
    assert review.sent_back_notice(session, task) is None


def test_age_text_is_in_words():
    now = utc_now()

    assert review.age_text(now) == "just now"
    assert review.age_text(now - timedelta(minutes=25)) == "25 minutes ago"
    assert review.age_text(now - timedelta(hours=1, minutes=5)) == "1 hour ago"
    assert review.age_text(now - timedelta(days=3, hours=2)) == "3 days ago"
    assert review.age_text(None) is None


# --- where it shows ------------------------------------------------------------------------


def test_the_timeline_ignores_notifications(session):
    import timeline

    task = add_task(session, status="in_progress")
    now = utc_now()
    for kind, day in (("start", 10), ("review_requested", 2), ("review_seen", 1)):
        session.add(TaskEvent(task_id=task.id, actor="Sam Example", from_status="in_progress",
                              to_status="in_progress", kind=kind, created_at=now - timedelta(days=day)))
    session.commit()

    found = timeline.moments(stored(session, TaskEvent))

    assert [m.kind for m in found] == ["start"]


def test_the_history_shows_the_message_and_never_the_stored_json(session, as_user):
    task = add_task(session)
    review.notify(session, task, MEMBER, "Please confirm the hole spacing")

    page = as_user(MEMBER).get("/tasks/M06").text

    assert "Sam Example notified a lead about “Drawing”" in page
    assert "Please confirm the hole spacing" in page
    assert '{"stage"' not in page and "&#34;stage&#34;" not in page
    (event,) = kinds(session, "review_requested")
    assert taskpage.event_text(event) == "Sam Example notified a lead about “Drawing”"


def test_history_for_a_member_never_says_approved_anywhere_on_the_pages(session, as_user):
    task = add_task(session, status="needs_review")
    task.stage = "Lead review"
    session.add(task)
    session.commit()
    client = as_user(MEMBER)
    client.post("/tasks/M06/status", data={"action": "advance", "step": "Lead review"}, follow_redirects=False)

    for path in ("/tasks/M06", "/", "/tasks", "/timeline", "/record"):
        text = client.get(path).text.lower()
        assert "approved" not in text and "self-approved" not in text, path
    assert "moved on from “lead review”" in client.get("/tasks/M06").text.lower()
