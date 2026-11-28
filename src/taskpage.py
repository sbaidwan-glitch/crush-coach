"""What the task page and Home show about a task, beyond the coach chat itself.

Presentation only: the stage strip, the History list, the "Next step" line pulled out
of the coach's stored answers, and how a coach message splits into its parts. Nothing
here calls the model; it reads what is already in the database.
"""

import logging
import re
from datetime import datetime

from sqlmodel import Session, col, select

import plan as plan_shape
import profiles
import review
import steps
import workflow
from auth import User
from models import (
    DECISION_STATUS_LABELS,
    TASK_TYPES,
    CoachMessage,
    Task,
    TaskEvent,
    WorkNote,
)

log = logging.getLogger("crush_coach")

def stage_strip(task: Task) -> list[dict]:
    """The task's steps, each marked past, current or ahead (see steps.py: the position
    always agrees with the status, and a finished task has every step past)."""
    return steps.strip(task)


def short_date(when: datetime) -> str:
    return f"{when.day} {when:%b}"


def event_text(event: TaskEvent) -> str:
    """One History line, in words: who did what. Nobody approves anything any more, so a
    member who moves on from a review stage "moved on from" it. Older rows (`approve`,
    `self_approve`, a send back with no reason) stay readable as they were."""
    who = event.actor
    step = f"“{event.detail}”" if event.detail else None
    match event.kind:
        case "advance":
            if event.from_status == "needs_review":  # moved on from a review stage
                moved = f"{who} moved on from {step}" if step else f"{who} moved on"
            else:
                moved = f"{who} finished {step}" if step else f"{who} finished a step"
            if event.to_status == "needs_review":
                return f"{moved} and moved it to a review stage"
            if event.to_status == "done":
                return f"{moved}; the task is complete"
            return moved
        case "approve":  # before leads stopped being gates
            return f"{who} approved {step}" if step else f"{who} approved it"
        case "self_approve":
            return f"{who} self-approved {step}" if step else f"{who} self-approved it"
        case "send_back":
            return f"{who} sent it back to {step}" if step else f"{who} sent it back"
        case "review_requested":
            stage = review.meta(event).get("stage")
            return f"{who} notified a lead about “{stage}”" if stage else f"{who} notified a lead"
        case "review_seen":
            return f"{who} saw the notification"
        case "change_type":
            return f"{who} changed the type: {event.detail}"
        case "edit_steps":
            return f"{who} edited the steps"
    # The oldest rows have no kind: say it from the statuses, as it was then.
    if event.to_status == "in_progress" and event.from_status == "needs_review":
        return f"{who} sent it back"
    if event.to_status == "in_progress":
        return f"{who} started the task"
    if event.to_status == "needs_review":
        return f"{who} sent it for review"
    if event.to_status == "done":
        return f"{who} approved it"
    if event.to_status == "todo":
        return f"{who} unclaimed the task"
    return f"{who} changed the status"


def note_text(note: WorkNote) -> str:
    """One History line for a work note: who logged work."""
    return f"{note.member_name} logged work"


def history(session: Session, task_id: int) -> list[dict]:
    """The task's moves and the work notes members logged on it, newest first."""
    events = session.exec(select(TaskEvent).where(TaskEvent.task_id == task_id)).all()
    notes = session.exec(  # a draft is its member's alone: only published entries show
        select(WorkNote).where(WorkNote.task_id == task_id, WorkNote.state == "published")
    ).all()
    rows: list[tuple[datetime, int, int, dict]] = [
        (
            e.created_at,
            0,
            e.id or 0,
            {
                "date": short_date(e.created_at),
                "text": event_text(e),
                # A lead's reason for sending it back. (A notification keeps its stage and
                # entry in `note`, not words.)
                "note": None if e.kind == "review_requested" else e.note,
                # A second line: the new list of steps, or a member's message to a lead.
                "detail": e.detail if e.kind in ("edit_steps", "review_requested") else None,
            },
        )
        for e in events
    ]
    rows.extend(
        (
            n.posted_at,
            1,
            n.id or 0,
            {
                "date": short_date(n.posted_at),
                "text": note_text(n),
                "note": None,
                # What they did, and why: the same words the Crush document gets.
                "detail": n.did + (f" Why: {n.why}" if n.why else ""),
            },
        )
        for n in notes
    )
    rows.sort(key=lambda row: row[:3], reverse=True)
    return [row[3] for row in rows]


def owner_since(session: Session, task: Task) -> str | None:
    """When the current owner started: the latest "started" event."""
    if not task.owner:
        return None
    event = session.exec(
        select(TaskEvent)
        .where(
            TaskEvent.task_id == task.id,
            TaskEvent.to_status == "in_progress",
            TaskEvent.from_status == "todo",  # a start, not a step finished or a send back
        )
        .order_by(col(TaskEvent.created_at).desc(), col(TaskEvent.id).desc())
    ).first()
    return short_date(event.created_at) if event else None


def step_note(task: Task) -> str | None:
    """What the main button will do, in words, for a task that is being worked on or is on
    a review stage."""
    current = steps.current_step(task)
    if task.status not in ("in_progress", "needs_review") or current is None:
        return None
    following = steps.next_step(task)
    if current.review:
        lead = (
            f"“{current.name}” is a review stage: talk to a lead, or notify one from your "
            "Notebook."
        )
        if following is None:
            return f"{lead} Finish the task when you are ready."
        return f"{lead} Move to “{following.name}” when you are ready."
    if following is None:
        return f"“{current.name}” is the last stage. Finish the task when the work is done."
    if following.review:
        return (
            f"Next is “{following.name}”. Moving there doesn’t send anything: talk to a "
            "lead, or notify one from your Notebook."
        )
    return f"Moving on takes the task from “{current.name}” to “{following.name}”."


def main_action(task: Task, actions: list[str]) -> tuple[str, str] | None:
    """The one main button on the task page: Start, then Move to next stage (Finish task on
    the last stage). None when nothing is for this person to press."""
    if "start" in actions:
        return "start", "Start"
    if "advance" in actions:
        return "advance", "Move to next stage" if steps.next_step(task) else "Finish task"
    return None


def page_context(session: Session, task: Task, user: User) -> dict:
    """Extra context for task.html. A failure here costs only its own section."""
    current = steps.current_step(task)
    available = workflow.available_actions(task, user)
    context: dict = {
        "stages": stage_strip(task),
        "step_name": current.name if current else steps.steps_for(task)[-1].name,
        "step_note": step_note(task),
        "actions": available,
        "main_action": main_action(task, available),
        "task_types": [(t, steps.TYPE_HINTS.get(t, "")) for t in TASK_TYPES],
        "step_rows": steps.rows_for(task),
        "custom_steps": steps.custom_steps(task) is not None,
        "steps_draft": None,
        "steps_error": None,
        "is_lead": user["role"] == "lead",
        "history": [],
        "history_error": None,
        "owner_since": None,
        "owner_chips": [],
        "review": None,
        "sent_back": None,
        "decision_status_labels": DECISION_STATUS_LABELS,
    }
    try:
        context["review"] = review.status_for(session, task)
        context["sent_back"] = review.sent_back_notice(session, task)
        context["history"] = history(session, task.id)
        context["owner_since"] = owner_since(session, task)
        context["owner_chips"] = profiles.load(session).chips(task.owner)
    except Exception as exc:  # noqa: BLE001  (the rest of the page still works)
        session.rollback()
        log.error("Could not load task history (%s)", type(exc).__name__)
        context["history_error"] = "Couldn't load this task's history right now."
    return context


# --- the coach's "Next step:" line ---

_NEXT_STEP = re.compile(r"^[\s>*_#-]*next step[\s*_]*:[\s*_]*(.*)$", re.IGNORECASE)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _plain(text: str) -> str:
    """Inline Markdown to plain text: links keep their words, emphasis marks go."""
    text = _LINK.sub(r"\1", text)
    return " ".join(text.replace("*", "").replace("`", "").split())


def next_step_from(content: str) -> str | None:
    """The text of the last "Next step:" line in a coach answer (first letter capital)."""
    found = None
    for line in (content or "").splitlines():
        match = _NEXT_STEP.match(line)
        if match and _plain(match.group(1)):
            text = _plain(match.group(1))
            found = text[0].upper() + text[1:]
    return found


def latest_next_steps(session: Session, task_ids: list[int]) -> dict[int, dict]:
    """task id -> {"text", "date"} from the newest coach answer on each task.

    Only the newest answer counts: if it has no "Next step:" line there is none, an
    older answer's step may be out of date. Reads stored messages; no model call.
    """
    found: dict[int, dict] = {}
    for task_id in task_ids:
        message = session.exec(
            select(CoachMessage)
            .where(CoachMessage.task_id == task_id, CoachMessage.role == "assistant")
            .order_by(col(CoachMessage.created_at).desc(), col(CoachMessage.id).desc())
        ).first()
        if message is None:
            continue
        text = next_step_from(message.content)
        if not text:  # a plan has no "Next step:" line: its "Do this now" is the step
            text = _plain(plan_shape.do_this_now(message.content)) or None
            text = text[0].upper() + text[1:] if text else None
        if text:
            found[task_id] = {"text": text, "date": short_date(message.created_at)}
    return found


# --- coach messages ---

_BLOCK_BREAK = re.compile(r"\n\s*\n")
_CONFLICT = re.compile(r"^[\s*_]*likely current\b", re.IGNORECASE)


def plan_parts(content: str) -> list[dict]:
    """The labelled parts of a "Plan my next steps" answer (see `plan.py`), or an empty
    list when the content isn't shaped like one. Presentation only."""
    return plan_shape.parse(content)


def message_parts(content: str) -> list[dict]:
    """Split a coach answer into the parts the design styles differently.

    Returns dicts with `kind` and `text`: "body" (Markdown, joined paragraphs),
    "conflict" (a paragraph that leads with "Likely current"), and "next" (the
    "Next step:" line, text without its label). Anything else stays body text, so an
    answer that has neither is one body part, exactly as written.
    """
    parts: list[dict] = []
    body: list[str] = []

    def flush() -> None:
        if body:
            parts.append({"kind": "body", "text": "\n\n".join(body)})
            body.clear()

    for block in _BLOCK_BREAK.split((content or "").strip()):
        lines = block.strip().splitlines()
        if not lines:
            continue
        step = _NEXT_STEP.match(lines[-1])
        if _CONFLICT.match(lines[0]):
            flush()
            parts.append({"kind": "conflict", "text": block.strip()})
        elif step and _plain(step.group(1)):
            # The step may share a paragraph with the lines before it.
            before = "\n".join(lines[:-1]).strip()
            if before:
                body.append(before)
            flush()
            parts.append({"kind": "next", "text": step.group(1).strip()})
        else:
            body.append(block.strip())
    flush()
    return parts

