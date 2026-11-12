"""The task status flow and who may move a task.

    todo -> in_progress -> needs_review -> done
                  ^______________|  (a lead sends it back)

Members move their own tasks: "Start this task" (which claims an unowned task),
"Ready for review", "Unclaim". Leads approve or send back. Only the owner or a lead
can move a task, and only leads approve. The server checks every move; the page
only hides buttons that wouldn't work. Each move is written to `task_event`, which
the task page shows as History.

`move` takes plain values and a Session, not a Request, so tests can use it directly.
"""

import logging
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session, select

import db
from auth import CurrentUser, User
from models import Task, TaskEvent, utc_now
from progress import owned_by

log = logging.getLogger("crush_coach")
router = APIRouter()

MAX_NOTE_CHARS = 500
DB_ERROR = "Couldn't reach the database right now. Nothing was changed. Try again."

# action -> (statuses it can start from, status it ends in, leads only?)
ACTIONS = {
    "start": (("todo",), "in_progress", False),
    "ready": (("in_progress",), "needs_review", False),
    "unclaim": (("todo", "in_progress"), "todo", False),
    "approve": (("needs_review",), "done", True),
    "send_back": (("needs_review",), "in_progress", True),
}


class MoveError(Exception):
    """A move that isn't allowed. The message is safe to show; `status` is the HTTP code."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.message = message
        self.status = status


def can_move(task: Task, user: User) -> bool:
    """The owner or a lead."""
    return user["role"] == "lead" or owned_by(task, user["name"], user["email"])


def check(task: Task, user: User, action: str) -> None:
    """Raise MoveError unless this person may take this action on this task now."""
    if action not in ACTIONS:
        raise MoveError("That isn't something you can do to a task.", 400)
    from_statuses, _to, leads_only = ACTIONS[action]
    if leads_only and user["role"] != "lead":
        raise MoveError("Only a lead can approve a task or send it back.")
    claiming = action == "start" and not task.owner
    if not claiming and not can_move(task, user):
        raise MoveError("Only the person who owns this task, or a lead, can change it.")
    if task.status not in from_statuses:
        raise MoveError(
            "This task has already changed. Reload the page to see where it is now.",
            409,
        )
    if action == "unclaim" and not task.owner:
        raise MoveError("Nobody owns this task.", 409)


def available_actions(task: Task, user: User) -> list[str]:
    """The actions this person may take on this task right now (what the buttons show)."""
    found = []
    for action in ACTIONS:
        try:
            check(task, user, action)
        except MoveError:
            continue
        found.append(action)
    return found


def move(
    session: Session, task: Task, user: User, action: str, note: str | None = None
) -> TaskEvent:
    """Apply one action and record it. Raises MoveError if it isn't allowed.

    Commits on success. The caller handles database failures.
    """
    check(task, user, action)
    _from, to_status, _leads_only = ACTIONS[action]
    from_status = task.status
    task.status = to_status
    if action == "start" and not task.owner:
        task.owner = user["name"]
    elif action == "unclaim":
        task.owner = None
    note = (note or "").strip()[:MAX_NOTE_CHARS] or None
    event = TaskEvent(
        task_id=task.id,
        actor=user["name"],
        from_status=from_status,
        to_status=to_status,
        note=note if action == "send_back" else None,
        created_at=utc_now(),
    )
    session.add(task)
    session.add(event)
    session.commit()
    return event


def _safe_next(value: str, fallback: str) -> str:
    """A local path to return to, never another site."""
    parts = urlsplit(value)
    if (
        value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and not parts.scheme
        and not parts.netloc
    ):
        return value
    return fallback


@router.post("/tasks/{code}/status", response_class=Response)
def change_status(
    request: Request,
    code: str,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    action: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    back = _safe_next(next, f"/tasks/{code}")

    def fail(message: str, status: int) -> Response:
        import coach  # here, not at the top: coach imports the task page helpers

        # The task page says what went wrong; the rest of it still works.
        session.rollback()
        response = coach._render_page(request, session, user, code, error=message)
        response.status_code = status
        return response

    try:
        task = session.exec(select(Task).where(Task.code == code)).first()
        if task is None:
            raise HTTPException(status_code=404)
        move(session, task, user, action, note)
    except HTTPException:
        raise
    except MoveError as exc:
        return fail(exc.message, exc.status)
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Could not change task status (%s)", type(exc).__name__)
        return fail(DB_ERROR, 503)
    return RedirectResponse(back, status_code=303)
