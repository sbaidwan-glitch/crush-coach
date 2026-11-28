"""Telling a lead about a task, and what the lead does with it.

Leads are not gates. A member moves their own task through every stage, including a
"Lead review" stage, because the member and the lead usually talk in person. All the app
does is let the member **notify** a lead (with an optional message), and show the lead what
to look at. Nothing waits on a lead.

Everything here is `TaskEvent` rows (no table of its own):

- `review_requested`: the member pressed "Notify team lead for review" in the Notebook.
  `detail` is their message, `note` is a small JSON object with the task's stage at the
  time and the Notebook entry it links to (`meta`). The task's status is not touched.
- `review_seen`: a lead pressed "Seen". Answers the notifications before it.
- `send_back`: a lead sent the task back, with a reason in `note`. Also answers them.
  (Older `approve` / `self_approve` rows answer them too.)

A notification is **unanswered** until a lead's answer comes after it. The Inbox shows one
card per task that has an unanswered notification; the sidebar's count and Home's "Your
tasks in review" tile count the same tasks. Pure rules plus plain queries; no model calls.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlmodel import Session, col, func, select

import localtime
import steps as stepslib
import workflow
from auth import User
from models import Task, TaskEvent, WorkNote, utc_now
from progress import SUBSYSTEM_GROUPS

REQUESTED = "review_requested"
SEEN = "review_seen"
SENT_BACK = "send_back"
# Kinds that answer a notification. The last two are from before leads stopped being gates.
ANSWERS = (SEEN, SENT_BACK, "approve", "self_approve")
KINDS = (REQUESTED, *ANSWERS)
# Kinds that are not a move: they never change a task's status or step.
NOT_MOVES = (REQUESTED, SEEN)

MAX_MESSAGE_CHARS = 280
MAX_REASON_CHARS = 200
DAILY_LIMIT = 3  # notifications per task per day


class ReviewError(Exception):
    """Something that can't be done. The message is safe to show; `status` is the HTTP code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# --- what is stored in an event ---------------------------------------------------------


def meta(event: TaskEvent) -> dict[str, Any]:
    """{"stage": ..., "entry": ...} from a `review_requested` event (empty if unreadable)."""
    try:
        found = json.loads(event.note or "")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _meta_json(stage: str, entry_id: int | None) -> str:
    return json.dumps({"stage": stage, "entry": entry_id}, separators=(",", ":"))


# --- who is waiting, and how long ago --------------------------------------------------------


def open_requests(session: Session, task_id: int) -> list[TaskEvent]:
    """The task's unanswered notifications, oldest first."""
    events = session.exec(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id, col(TaskEvent.kind).in_(KINDS))
        .order_by(col(TaskEvent.id))
    ).all()
    return _unanswered(events)


def _unanswered(events: list[TaskEvent]) -> list[TaskEvent]:
    found: list[TaskEvent] = []
    for event in events:  # in the order they happened
        if event.kind == REQUESTED:
            found.append(event)
        else:
            found.clear()
    return found


def unanswered_by_task(session: Session) -> dict[int, list[TaskEvent]]:
    """task id -> its unanswered notifications (oldest first), for every task that has one."""
    rows = session.exec(
        select(TaskEvent)
        .join(Task, col(Task.id) == col(TaskEvent.task_id))
        .where(col(TaskEvent.kind).in_(KINDS))
        .order_by(col(TaskEvent.task_id), col(TaskEvent.id))
    ).all()
    grouped: dict[int, list[TaskEvent]] = {}
    for event in rows:
        grouped.setdefault(event.task_id, []).append(event)
    found = {task_id: _unanswered(events) for task_id, events in grouped.items()}
    return {task_id: events for task_id, events in found.items() if events}


def count(session: Session) -> int:
    """How many tasks have an unanswered notification: the Inbox's "Work to check" cards."""
    return len(unanswered_by_task(session))


def waiting_task_ids(session: Session) -> set[int]:
    return set(unanswered_by_task(session))


def age_text(since: datetime | None) -> str | None:
    """"just now", "25 minutes ago", "3 hours ago", "2 days ago"."""
    if since is None:
        return None
    seconds = max(0, int((utc_now() - since).total_seconds()))
    if seconds < 90:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


# --- the member notifies a lead -----------------------------------------------------------


@dataclass
class Notified:
    """What pressing the button did. `sent` is False when a notification was already
    waiting (`event` is that one): nothing was added."""

    sent: bool
    event: TaskEvent

    @property
    def clock(self) -> str:
        return localtime.clock(self.event.created_at)


def sent_today(session: Session, task_id: int) -> int:
    return session.exec(
        select(func.count())
        .select_from(TaskEvent)
        .where(
            TaskEvent.task_id == task_id,
            TaskEvent.kind == REQUESTED,
            TaskEvent.created_at >= localtime.day_start_utc(),
        )
    ).one()


def notify(
    session: Session,
    task: Task,
    user: User,
    message: str = "",
    *,
    entry_id: int | None = None,
    follow_up: bool = False,
) -> Notified:
    """A member notifies a lead about a task. Commits when it adds an event.

    With a notification already waiting, nothing is added (`sent` is False) unless this
    is a follow-up. At most `DAILY_LIMIT` per task per day, checked here, not on the page.
    Raises ReviewError.
    """
    if not workflow.can_move(task, user):
        raise ReviewError("Only the person who owns this task, or a lead, can notify a lead about it.", 403)
    message = " ".join((message or "").split())
    if len(message) > MAX_MESSAGE_CHARS:
        raise ReviewError(f"Keep the message to {MAX_MESSAGE_CHARS} characters or fewer.")
    waiting = open_requests(session, task.id)
    if waiting and not follow_up:
        return Notified(False, waiting[-1])
    if sent_today(session, task.id) >= DAILY_LIMIT:
        raise ReviewError(
            f"You have already notified a lead about {task.code} {DAILY_LIMIT} times today. "
            "Talk to them in person, or try again tomorrow.",
            429,
        )
    steps = stepslib.steps_for(task)
    here = stepslib.current_index(task, steps)
    stage = steps[min(here, len(steps) - 1)].name
    event = TaskEvent(
        task_id=task.id,
        actor=user["name"],
        from_status=task.status,
        to_status=task.status,
        kind=REQUESTED,
        detail=message or None,
        note=_meta_json(stage, entry_id),
        created_at=utc_now(),
    )
    session.add(event)
    session.commit()
    return Notified(True, event)


def status_for(session: Session, task: Task) -> dict[str, Any]:
    """What the Notebook and the task page say about this task's notifications."""
    waiting = open_requests(session, task.id)
    used = sent_today(session, task.id)
    last = waiting[-1] if waiting else None
    return {
        "waiting": last is not None,
        "clock": localtime.clock(last.created_at) if last else None,
        "message": last.detail if last else None,
        "used": used,
        "limit": DAILY_LIMIT,
        "limit_reached": used >= DAILY_LIMIT,
    }


def sent_text(done: Notified) -> str:
    if done.sent:
        return f"Your team lead can see this in their inbox. Sent at {done.clock}."
    return f"Already sent at {done.clock}."


# --- the lead's side -----------------------------------------------------------------------


def _part_slug(subsystem: str) -> str | None:
    for group in SUBSYSTEM_GROUPS:
        if subsystem in group["subsystems"]:
            return group["slug"]
    return None


def entry_link(session: Session, entry_id: int | None) -> dict[str, str] | None:
    """Where a lead can read the entry a notification links to. A draft is its member's
    alone, so only a published entry has a link; a draft is named without one."""
    if not entry_id:
        return None
    note = session.get(WorkNote, entry_id)
    if note is None:
        return None
    if note.state != "published":
        return {"label": "Entry (still a draft, not shared yet)", "href": ""}
    part = _part_slug(note.subsystem)
    href = f"/record?part={part}&notes=1#note-{note.id}" if part else ""
    return {"label": "Entry", "href": href}


def cards(session: Session) -> list[dict[str, Any]]:
    """One card per task with an unanswered notification, longest wait first."""
    found = unanswered_by_task(session)
    if not found:
        return []
    tasks = {
        t.id: t for t in session.exec(select(Task).where(col(Task.id).in_(list(found)))).all()
    }
    result = []
    for task_id, events in found.items():
        task = tasks.get(task_id)
        if task is None:
            continue
        first, last = events[0], events[-1]
        info = meta(last)
        result.append(
            {
                "kind": "work",  # the Inbox's name for this card
                "id": last.id,  # what Seen and Send back post
                "task": task,
                "member": last.actor,
                "stage": info.get("stage") or task.stage,
                "message": last.detail,
                "earlier": [e.detail for e in events[:-1] if e.detail],
                "sent": len(events),
                "age": age_text(first.created_at),
                "since": first.created_at,
                "entry": entry_link(session, info.get("entry")),
            }
        )
    result.sort(key=lambda c: c["since"])
    return result


def _waiting_event(session: Session, lead: User, event_id: int) -> TaskEvent:
    if lead["role"] != "lead":
        raise ReviewError("Only a lead can answer a notification.", 403)
    event = session.get(TaskEvent, event_id)
    if event is None or event.kind != REQUESTED:
        raise ReviewError("That notification isn't there any more.", 404)
    waiting = open_requests(session, event.task_id)
    if not any(w.id == event.id for w in waiting):
        raise ReviewError("A lead has already answered this. Reload the page.", 409)
    return event


def mark_seen(session: Session, lead: User, event_id: int) -> TaskEvent:
    """"Seen": the card goes away. The task is not moved."""
    event = _waiting_event(session, lead, event_id)
    task = session.get(Task, event.task_id)
    answer = TaskEvent(
        task_id=event.task_id,
        actor=lead["name"],
        from_status=task.status if task else event.to_status,
        to_status=task.status if task else event.to_status,
        kind=SEEN,
        created_at=utc_now(),
    )
    session.add(answer)
    session.commit()
    return answer


def send_back_target(task: Task, stage: str | None) -> str | None:
    """The working step a task goes back to, or None when it stays where it is.

    The review step is the one on or before the stage the member notified from (or the
    next one ahead, when they notified before reaching it). A task still short of that
    step has nothing to go back from. A task on it, or past it (finished included), goes
    to the working step just before it."""
    steps = stepslib.steps_for(task)
    here = stepslib.current_index(task, steps)
    noted = stepslib.match_stage(stage, steps)
    reviews = [i for i, s in enumerate(steps) if s.review]
    if not reviews:
        return None
    start = noted if noted is not None else here
    behind = [i for i in reviews if i <= start]
    review = behind[-1] if behind else reviews[0]
    if here < review:
        return None
    return steps[stepslib.work_index(steps, review - 1)].name


def send_back(session: Session, lead: User, event_id: int, reason: str) -> TaskEvent:
    """A lead sends the task back with a one-line reason. If the task is still on or past
    the review stage it goes back to the working stage before it; otherwise it stays where
    it is and only the reason is recorded. The member sees the reason on Home."""
    event = _waiting_event(session, lead, event_id)
    reason = " ".join((reason or "").split())
    if not reason:
        raise ReviewError("Say in a line why you are sending it back.")
    if len(reason) > MAX_REASON_CHARS:
        raise ReviewError(f"Keep the reason to {MAX_REASON_CHARS} characters or fewer.")
    task = session.get(Task, event.task_id)
    if task is None:
        raise ReviewError("That task isn't there any more.", 404)
    from_status = task.status
    target = send_back_target(task, meta(event).get("stage"))
    if target is not None:
        task.status = "in_progress"
        task.stage = target
        session.add(task)
    answer = TaskEvent(
        task_id=task.id,
        actor=lead["name"],
        from_status=from_status,
        to_status=task.status,
        note=reason,
        kind=SENT_BACK,
        detail=target,
        created_at=utc_now(),
    )
    session.add(answer)
    session.commit()
    return answer


# --- the member sees it come back ----------------------------------------------------------


def sent_back_notice(session: Session, task: Task) -> dict[str, str] | None:
    """The reason a lead sent this task back, until the member moves it on, starts it
    again or notifies a lead again; None if there is nothing to show."""
    latest = session.exec(
        select(TaskEvent)
        .where(
            TaskEvent.task_id == task.id,
            col(TaskEvent.kind).in_(("advance", "start", "unclaim", REQUESTED, SENT_BACK)),
        )
        .order_by(col(TaskEvent.id).desc())
    ).first()
    if latest is None or latest.kind != SENT_BACK or not latest.note:
        return None
    return {"reason": latest.note, "by": latest.actor, "stage": latest.detail or ""}
