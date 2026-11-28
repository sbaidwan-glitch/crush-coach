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


# --- the button ------------------------------------------------------------------------


def test_the_entry_form_has_a_blue_notify_button_beside_publish_and_no_message_box(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/notebook/panel?task=M06", headers=HTMX).text

    assert 'class="btn btn--notify"' in html and "Notify team lead for review</button>" in html
    assert 'formaction="/notebook/entry/notify"' in html and 'hx-post="/notebook/entry/notify"' in html
    # The message is asked for in a pop-up afterwards, not in the (messy) side panel.
    assert 'name="message"' not in html and "Please confirm the hole spacing" not in html
    assert "Publish to Crush doc</button>" in html
    assert "publishing doesn’t notify anyone" in html
    assert html.index("Publish to Crush doc</button>") < html.index("Notify team lead for review</button>")


def test_the_button_is_in_the_page_version_too(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/notebook?task=M06").text

    assert "Notify team lead for review</button>" in html and 'name="message"' not in html
    assert 'id="notify-modal"' not in html  # nothing pops up until it is pressed


def test_no_task_no_button(session, as_user):
    html = as_user(MEMBER).get("/notebook/panel", headers=HTMX).text  # owns nothing

    assert "Notify team lead for review" not in html


# --- pressing it -----------------------------------------------------------------------


def test_pressing_it_records_the_notification_and_pops_up_outside_the_side_panel(session, as_user, doc):
    task = add_task(session, status="needs_review")
    task.stage = "Lead review"
    session.add(task)
    session.commit()

    response = notify(as_user(MEMBER), did="half a thought")

    assert response.status_code == 200
    (event,) = kinds(session, "review_requested")
    assert event.actor == "Sam Example" and event.detail is None  # the pop-up asks for a message next
    assert review.meta(event) == {"stage": "Lead review", "entry": None}
    assert (event.from_status, event.to_status) == ("needs_review", "needs_review")
    session.expire_all()
    assert (task.status, task.stage) == ("needs_review", "Lead review")  # nothing moved
    assert stored(session, WorkNote) == [] and stored(session, DriveWrite) == []  # nothing saved or published
    assert doc.gets == 0
    panel, _, popup = response.text.partition('id="notify-slot"')
    assert "half a thought" in panel  # what they typed is still in the form
    # The pop-up is sent out of band to the page's slot, never inside the side panel.
    assert 'hx-swap-oob="true"' in popup and 'id="notify-modal"' in popup
    assert "Your team lead has been notified" not in panel and "Do you want to add a message" not in panel
    assert "Your team lead has been notified" in popup
    assert "Do you want to add a message for them?" in popup
    assert 'name="message"' in popup and 'maxlength="280"' in popup
    assert 'aria-label="Close"' in popup  # the X
    assert '<i data-seconds="10"></i>' in popup  # the 10-second bar at its foot


def test_the_page_has_one_slot_for_the_pop_up_outside_the_notebook_dialog(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/tasks/M06").text

    assert html.count('id="notify-slot"') == 1
    assert html.index('id="notify-slot"') < html.index('<dialog id="notebook"')  # a sibling, not inside it


def test_the_message_is_optional(session, as_user):
    add_task(session)

    notify(as_user(MEMBER))

    (event,) = kinds(session, "review_requested")
    assert event.detail is None


def test_a_message_sent_with_the_press_over_280_characters_is_refused(session, as_user):
    add_task(session)

    response = notify(as_user(MEMBER), message="x" * 281)

    assert response.status_code == 400 and "280 characters" in response.text
    assert kinds(session, "review_requested") == []


# --- the pop-up's message ---------------------------------------------------------------


def message_url(event):
    return f"/notebook/notify/{event.id}/message"


def test_send_message_adds_the_line_to_the_notification_and_says_so(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    notify(client)
    (event,) = kinds(session, "review_requested")

    response = client.post(message_url(event), data={"message": "  Please confirm the\nhole spacing "}, headers=HTMX)

    assert response.status_code == 200
    assert "Message sent" in response.text and "Your team lead can see it in their inbox." in response.text
    assert '<i data-seconds="3"></i>' in response.text  # closes itself sooner now
    assert "<dialog" not in response.text and "notify-slot" not in response.text  # only the card is swapped
    session.expire_all()
    assert kinds(session, "review_requested")[0].detail == "Please confirm the hole spacing"
    assert len(kinds(session, "review_requested")) == 1  # the same notification, not a new one


def test_an_empty_or_over_long_message_is_refused_and_the_pop_up_stays(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    notify(client)
    (event,) = kinds(session, "review_requested")

    empty = client.post(message_url(event), data={"message": "  "}, headers=HTMX)
    long = client.post(message_url(event), data={"message": "x" * 281}, headers=HTMX)

    assert empty.status_code == 400 and "Write a message first" in empty.text and 'name="message"' in empty.text
    assert long.status_code == 400 and "280 characters" in long.text and "Message sent" not in long.text
    session.expire_all()
    assert kinds(session, "review_requested")[0].detail is None


def test_a_message_of_exactly_280_is_fine(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    notify(client)
    (event,) = kinds(session, "review_requested")

    assert client.post(message_url(event), data={"message": "x" * 280}, headers=HTMX).status_code == 200


def test_only_the_owner_or_a_lead_can_add_to_it_and_not_after_a_lead_answered(session, as_user):
    task = add_task(session)
    owner = as_user(MEMBER)
    notify(owner)
    (event,) = kinds(session, "review_requested")

    stranger = as_user(OTHER).post(message_url(event), data={"message": "Mine now"}, headers=HTMX)
    assert stranger.status_code == 403
    assert as_user(LEAD).post(message_url(event), data={"message": "From a lead"}, headers=HTMX).status_code == 200

    review.mark_seen(session, LEAD, event.id)
    late = as_user(MEMBER).post(message_url(event), data={"message": "Too late"}, headers=HTMX)
    assert late.status_code == 409 and "already answered" in late.text
    session.expire_all()
    assert kinds(session, "review_requested")[0].detail == "From a lead"
    assert task.status == "in_progress"


def test_a_message_for_a_notification_that_does_not_exist_is_a_404(session, as_user):
    add_task(session)

    assert as_user(MEMBER).post("/notebook/notify/999/message", data={"message": "x"}, headers=HTMX).status_code == 404


def test_the_message_reaches_the_lead_in_the_inbox(session, as_user):
    add_task(session)
    notify(as_user(MEMBER))
    (event,) = kinds(session, "review_requested")
    as_user(MEMBER).post(message_url(event), data={"message": "Please confirm the hole spacing"}, headers=HTMX)

    html = as_user(LEAD).get("/inbox").text

    assert "Please confirm the hole spacing" in html and "Work to check" in html


def test_the_second_press_adds_nothing_and_the_pop_up_says_when_it_was_sent(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    notify(client)
    (first,) = kinds(session, "review_requested")

    again = notify(client)

    assert len(kinds(session, "review_requested")) == 1
    assert f"Your team lead was already notified at {localtime.clock(first.created_at)}" in again.text
    assert "Do you want to add a message for them?" in again.text  # and can still add or change one


def test_a_follow_up_adds_a_new_event(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    notify(client, message="First")

    response = notify(client, message="Any news?", follow_up="1")

    assert [e.detail for e in kinds(session, "review_requested")] == ["First", "Any news?"]
    assert "Your team lead has been notified" in response.text


def test_at_most_three_a_day_per_task_checked_on_the_server(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    notify(client, message="1")
    notify(client, message="2", follow_up="1")
    notify(client, message="3", follow_up="1")

    fourth = notify(client, message="4", follow_up="1")
    plain = notify(client, message="5")  # not a follow-up either: still can't go over

    assert fourth.status_code == 429 and "3 times today" in fourth.text
    assert plain.status_code == 200 and "already notified at" in plain.text
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


def test_without_javascript_the_page_comes_back_with_the_pop_up_open_and_the_text_kept(session, as_user):
    add_task(session)
    client = as_user(MEMBER)

    response = client.post(
        "/notebook/entry/notify", data={**HTMX_FORM, "did": "typed"}, follow_redirects=False
    )

    assert response.status_code == 200  # the page itself, not a redirect that would drop the text
    assert "typed" in response.text
    assert '<dialog class="modal" id="notify-modal" aria-labelledby="notify-title" open>' in response.text
    assert response.text.count('id="notify-slot"') == 1 and "Your team lead has been notified" in response.text
    (event,) = kinds(session, "review_requested")

    sent = client.post(message_url(event), data={"message": "Look"}, follow_redirects=False)

    assert sent.status_code == 200 and "Message sent" in sent.text and "<title>Notebook" in sent.text


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
