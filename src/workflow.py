"""The task flow: status, steps, and who may move a task.

    todo -> in_progress -> needs_review -> done
                  ^______________|  (a lead sends it back)

A task moves through the steps of its type (steps.py). The member's "Next step done"
finishes the current step; when the next step is a review step the task becomes
`needs_review`, and a lead's Approve moves past it (to `done` after the last step).
Status and step always move together, so they can't disagree.

Members move their own tasks: "Start this task" (which claims an unowned task), "Next
step done", "Unclaim". Leads approve or send back; a lead may approve their own task
and the History then says "self-approved". Leads also change a task's type and edit its
steps. The server checks every move; the page only hides buttons that wouldn't work.
Each move is written to `task_event`, which the task page shows as History.

`move` takes plain values and a Session, not a Request, so tests can use it directly.
"""

import logging
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session, select

import db
import shell
import steps as stepslib
from auth import CurrentUser, User
from models import TASK_TYPES, Task, TaskEvent, utc_now
from progress import owned_by

log = logging.getLogger("crush_coach")
router = APIRouter()
templates = shell.templates()

MAX_NOTE_CHARS = 500
DB_ERROR = "Couldn't reach the database right now. Nothing was changed. Try again."

# action -> (statuses it can start from, leads only?). Where it ends up depends on the
# task's steps: see `_destination`.
ACTIONS = {
    "start": (("todo",), False),
    "advance": (("in_progress",), False),
    "unclaim": (("todo", "in_progress"), False),
    "approve": (("needs_review",), True),
    "send_back": (("needs_review",), True),
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


def is_self_review(task: Task, user: User) -> bool:
    """A lead acting on a task they own."""
    return user["role"] == "lead" and owned_by(task, user["name"], user["email"])


def check(task: Task, user: User, action: str) -> None:
    """Raise MoveError unless this person may take this action on this task now."""
    if action not in ACTIONS:
        raise MoveError("That isn't something you can do to a task.", 400)
    from_statuses, leads_only = ACTIONS[action]
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


def _destination(task: Task, action: str) -> tuple[str, str]:
    """(status, stage) after this action. Status and stage always agree."""
    steps = stepslib.steps_for(task)
    here = stepslib.current_index(task, steps)
    if action == "start":
        return "in_progress", steps[stepslib.work_index(steps, here)].name
    if action == "unclaim":
        return "todo", steps[min(here, len(steps) - 1)].name
    if action == "send_back":
        return "in_progress", steps[stepslib.work_index(steps, here - 1)].name
    return stepslib.landing(steps, here + 1)  # advance, approve


def move(
    session: Session,
    task: Task,
    user: User,
    action: str,
    note: str | None = None,
    expected_step: str | None = None,
) -> TaskEvent:
    """Apply one action and record it. Raises MoveError if it isn't allowed.

    `expected_step` is the step the person saw when they clicked; if the task has moved
    since (a double click, someone else's move), the move is refused instead of
    skipping a step. Commits on success. The caller handles database failures.
    """
    check(task, user, action)
    steps = stepslib.steps_for(task)
    here = stepslib.current_index(task, steps)
    step_name = steps[here].name if here < len(steps) else steps[-1].name
    stale = expected_step and expected_step.casefold() != step_name.casefold()
    if stale and action in ("advance", "approve", "send_back"):
        raise MoveError(
            "This task has already changed. Reload the page to see where it is now.", 409
        )
    to_status, to_stage = _destination(task, action)
    from_status = task.status
    task.status = to_status
    task.stage = to_stage
    if action == "start" and not task.owner:
        task.owner = user["name"]
    elif action == "unclaim":
        task.owner = None
    note = (note or "").strip()[:MAX_NOTE_CHARS] or None
    kind = action
    detail = None
    if action in ("advance", "approve"):
        detail = step_name
    elif action == "send_back":
        detail = to_stage
    if action == "approve" and is_self_review(task, user):
        kind = "self_approve"
    event = TaskEvent(
        task_id=task.id,
        actor=user["name"],
        from_status=from_status,
        to_status=to_status,
        note=note if action == "send_back" else None,
        kind=kind,
        detail=detail,
        created_at=utc_now(),
    )
    session.add(task)
    session.add(event)
    session.commit()
    return event


def change_type(session: Session, task: Task, user: User, new_type: str) -> TaskEvent | None:
    """A lead changes the task's type. Returns None if it is already that type."""
    if user["role"] != "lead":
        raise MoveError("Only a lead can change a task's type.")
    if new_type not in TASK_TYPES:
        raise MoveError("Choose one of the task types.", 400)
    if new_type == task.task_type:
        return None
    old_type = task.task_type
    stage, status = stepslib.retype(task, new_type)
    event = TaskEvent(
        task_id=task.id,
        actor=user["name"],
        from_status=task.status,
        to_status=status,
        kind="change_type",
        detail=f"{old_type} → {new_type}",
        created_at=utc_now(),
    )
    task.task_type = new_type
    task.stage = stage
    task.status = status
    session.add(task)
    session.add(event)
    session.commit()
    return event


def edit_steps(
    session: Session, task: Task, user: User, rows: list[stepslib.Row]
) -> TaskEvent:
    """A lead saves a new list of steps. Raises MoveError (400) with the reason if the
    list can't be used."""
    if user["role"] != "lead":
        raise MoveError("Only a lead can edit a task's steps.")
    problem = stepslib.validate_rows(rows, task)
    if problem:
        raise MoveError(problem, 400)
    saved = stepslib.save_rows(task, rows)
    event = TaskEvent(
        task_id=task.id,
        actor=user["name"],
        from_status=task.status,
        to_status=task.status,
        kind="edit_steps",
        detail=stepslib.describe(saved),
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


def _load(session: Session, code: str) -> Task:
    task = session.exec(select(Task).where(Task.code == code)).first()
    if task is None:
        raise HTTPException(status_code=404)
    return task


def _fail_page(
    request: Request,
    session: Session,
    user: User,
    code: str,
    message: str,
    status: int,
    **extra,
) -> Response:
    import coach  # here, not at the top: coach imports the task page helpers

    # The task page says what went wrong; the rest of it still works.
    session.rollback()
    response = coach._render_page(request, session, user, code, error=message, extra=extra)
    response.status_code = status
    return response


@router.post("/tasks/{code}/status", response_class=Response)
def change_status(
    request: Request,
    code: str,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    action: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
    step: Annotated[str, Form()] = "",
):
    back = _safe_next(next, f"/tasks/{code}")
    try:
        move(session, _load(session, code), user, action, note, expected_step=step)
    except HTTPException:
        raise
    except MoveError as exc:
        return _fail_page(request, session, user, code, exc.message, exc.status)
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Could not change task status (%s)", type(exc).__name__)
        return _fail_page(request, session, user, code, DB_ERROR, 503)
    return RedirectResponse(back, status_code=303)


@router.post("/tasks/{code}/type", response_class=Response)
def change_task_type(
    request: Request,
    code: str,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    task_type: Annotated[str, Form()] = "",
):
    try:
        change_type(session, _load(session, code), user, task_type)
    except HTTPException:
        raise
    except MoveError as exc:
        return _fail_page(request, session, user, code, exc.message, exc.status)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not change task type (%s)", type(exc).__name__)
        return _fail_page(request, session, user, code, DB_ERROR, 503)
    return RedirectResponse(f"/tasks/{code}", status_code=303)


def _rows_from_form(names: list[str], origs: list[str], reviews: list[str]) -> list[stepslib.Row]:
    """The editor's rows as posted: parallel lists, and the row numbers that are ticked."""
    ticked = {r for r in reviews if r.isdigit()}
    return [
        stepslib.Row(
            name=name,
            review=str(i) in ticked,
            orig=int(origs[i]) if i < len(origs) and origs[i].isdigit() else None,
        )
        for i, name in enumerate(names)
    ]


@router.post("/tasks/{code}/steps", response_class=Response)
def edit_task_steps(
    request: Request,
    code: str,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    op: Annotated[str, Form()] = "save",
    name: Annotated[list[str], Form()] = [],  # noqa: B006  (FastAPI reads the default)
    orig: Annotated[list[str], Form()] = [],  # noqa: B006
    review: Annotated[list[str], Form()] = [],  # noqa: B006
):
    """The lead's step editor. Buttons that only rearrange the rows (add, move, remove,
    reset) show the editor again with the changes, saving nothing; Save checks the list
    and stores it. With htmx the editor alone comes back; otherwise the whole page."""
    htmx = request.headers.get("HX-Request") == "true"
    rows = _rows_from_form(name, orig, review)
    error = None
    try:
        task = _load(session, code)
        if user["role"] != "lead":
            raise MoveError("Only a lead can edit a task's steps.")
        if op and op != "save":
            rows = stepslib.apply_op(rows, op, task)
        else:
            edit_steps(session, task, user, rows)
            if htmx:  # reload the whole page so the strip and History update too
                return Response(status_code=204, headers={"HX-Redirect": f"/tasks/{code}"})
            return RedirectResponse(f"/tasks/{code}", status_code=303)
    except HTTPException:
        raise
    except MoveError as exc:
        if exc.status == 403:
            return _fail_page(request, session, user, code, exc.message, exc.status)
        error = exc.message
    except Exception as exc:  # noqa: BLE001
        log.error("Could not save task steps (%s)", type(exc).__name__)
        return _fail_page(request, session, user, code, DB_ERROR, 503)
    session.rollback()
    task = _load(session, code)
    if htmx:
        return templates.TemplateResponse(
            request,
            "_steps_editor.html",
            {"task": task, "rows": rows, "steps_error": error, "user": user},
        )
    return _fail_page(
        request, session, user, code, "", 400 if error else 200,
        steps_draft=rows, steps_error=error,
    )
