"""What the task page and Home show about a task, beyond the coach chat itself.

Presentation only: the stage strip, the History list, the "Next step" line pulled out
of the coach's stored answers, and how a coach message splits into its parts. Nothing
here calls the model; it reads what is already in the database.
"""

import logging
import re
from datetime import datetime

from sqlmodel import Session, col, select

import workflow
from auth import User
from models import (
    DECISION_STATUS_LABELS,
    CoachMessage,
    Task,
    TaskEvent,
)

log = logging.getLogger("crush_coach")

# The stages of each kind of task (CLAUDE.md), as (stored key, label). `Task.stage`
# holds the key, usually lower case ("drawing", "lead review", "CAD").
STAGES = {
    "Fabricate": (
        ("drawing", "Drawing"),
        ("ai pre-check", "AI pre-check"),
        ("lead review", "Lead review"),
        ("process router", "Process router"),
        ("shop", "Shop"),
        ("inspect", "Inspect"),
    ),
    "Design": (
        ("requirements", "Requirements"),
        ("concepts", "Concepts"),
        ("lead review", "Lead review"),
        ("cad", "CAD"),
        ("fit check", "Fit check"),
    ),
    "Validate": (
        ("test plan", "Test plan"),
        ("calculations", "Calculations"),
        ("lead approval", "Lead approval"),
        ("run", "Run"),
        ("log results", "Log results"),
    ),
}


def stage_strip(task: Task) -> list[dict]:
    """The stages for this task's type, each marked past, current or ahead.

    A stage word we don't recognise marks nothing: every step shows as ahead.
    """
    stages = STAGES.get(task.task_type, ())
    word = " ".join((task.stage or "").split()).casefold()
    current = next(
        (i for i, (key, _label) in enumerate(stages) if word and word.startswith(key)),
        None,
    )
    return [
        {
            "label": label,
            "state": "current"
            if i == current
            else "past"
            if current is not None and i < current
            else "ahead",
        }
        for i, (_key, label) in enumerate(stages)
    ]


def short_date(when: datetime) -> str:
    return f"{when.day} {when:%b}"


def event_text(event: TaskEvent) -> str:
    """One History line, in words: who did what."""
    who = event.actor
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


def history(session: Session, task_id: int) -> list[dict]:
    events = session.exec(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(col(TaskEvent.created_at).desc(), col(TaskEvent.id).desc())
    ).all()
    return [
        {"date": short_date(e.created_at), "text": event_text(e), "note": e.note}
        for e in events
    ]


def owner_since(session: Session, task: Task) -> str | None:
    """When the current owner started: the latest "started" event."""
    if not task.owner:
        return None
    event = session.exec(
        select(TaskEvent)
        .where(TaskEvent.task_id == task.id, TaskEvent.to_status == "in_progress")
        .order_by(col(TaskEvent.created_at).desc(), col(TaskEvent.id).desc())
    ).first()
    return short_date(event.created_at) if event else None


def page_context(session: Session, task: Task, user: User) -> dict:
    """Extra context for task.html. A failure here costs only its own section."""
    context: dict = {
        "stages": stage_strip(task),
        "actions": workflow.available_actions(task, user),
        "is_lead": user["role"] == "lead",
        "history": [],
        "history_error": None,
        "owner_since": None,
        "decision_status_labels": DECISION_STATUS_LABELS,
    }
    try:
        context["history"] = history(session, task.id)
        context["owner_since"] = owner_since(session, task)
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
        if text:
            found[task_id] = {"text": text, "date": short_date(message.created_at)}
    return found


# --- coach messages ---

_BLOCK_BREAK = re.compile(r"\n\s*\n")
_CONFLICT = re.compile(r"^[\s*_]*likely current\b", re.IGNORECASE)


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

