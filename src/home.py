"""Home (`/`): what should I do next?

Three tiles (notes this week, your tasks a lead hasn't seen yet, available tasks), Crush's build
progress (render with a tag per part, gauge, a bar per part), your active task (the stored
"Next step" from the coach, never a fresh model call) and a few unclaimed tasks to claim.
Signed-out visitors see the same page as a read-only preview: progress is real, tasks
are blurred stand-ins, and nothing can be claimed.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

import auth
import board
import db
import notebook
import progress
import review
import shell
import steps
import taskpage
import workflow
from models import STATUS_LABELS, Task

log = logging.getLogger("crush_coach")
templates = shell.templates()
router = APIRouter()

PICK_UP_LIMIT = 4


def my_task(tasks: list[Task], user: auth.User) -> tuple[Task | None, int]:
    """The member's current task, and how many more they have.

    A task in progress comes first; one on a review stage is shown if nothing else is.
    """
    mine = [t for t in tasks if progress.owned_by(t, user["name"], user["email"])]
    for status in ("in_progress", "needs_review"):
        current = [t for t in mine if t.status == status]
        if current:
            others = len([t for t in mine if t.status in progress.STARTED]) - 1
            return current[0], others
    return None, 0


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
):
    return render_home(request, session, auth.session_user(request))


def render_home(
    request: Request,
    session: Session,
    user: auth.User | None,
    *,
    notice: str | None = None,
    status: int | None = None,
) -> HTMLResponse:
    """The Home page. `notice` says what went wrong with a move made from here (the
    rest of the page still shows)."""
    preview = user is None

    error = None
    tasks: list[Task] = []
    try:
        tasks = board.load_tasks(session)
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Could not load tasks (%s)", type(exc).__name__)
        error = "Couldn't load tasks from the database right now. Try again in a moment."

    current, more = (None, 0) if preview else my_task(tasks, user)
    next_step = None
    if current is not None:
        try:
            next_step = taskpage.latest_next_steps(session, [current.id]).get(current.id)
        except Exception as exc:  # noqa: BLE001  (the rest of the page still works)
            session.rollback()
            log.error("Could not load the next step (%s)", type(exc).__name__)

    sent_back = None
    if current is not None:
        try:
            sent_back = review.sent_back_notice(session, current)
        except Exception as exc:  # noqa: BLE001  (the rest of the page still works)
            session.rollback()
            log.error("Could not load the sent-back note (%s)", type(exc).__name__)

    in_review = 0  # tasks of the member's that a lead has been notified about and not answered
    if not preview:
        try:
            waiting = review.waiting_task_ids(session)
            in_review = sum(
                1
                for t in tasks
                if t.id in waiting and progress.owned_by(t, user["name"], user["email"])
            )
        except Exception as exc:  # noqa: BLE001  (the tile shows 0; the rest still works)
            session.rollback()
            log.error("Could not count the notifications (%s)", type(exc).__name__)

    notes_week: int | None = None
    my_drafts = 0
    if not preview:
        try:
            notes_week, my_drafts = notebook.week_counts(session, user)
        except Exception as exc:  # noqa: BLE001  (the tile shows a dash; the rest still works)
            session.rollback()
            log.error("Could not count this week's notes (%s)", type(exc).__name__)

    groups = progress.group_progress(tasks)
    unclaimed = [t for t in tasks if progress.is_unclaimed(t)]
    context = {
        "user": user,
        "error": error,
        "task": current,
        "more_tasks": more,
        "next_step": next_step,
        "stages": taskpage.stage_strip(current) if current else [],
        "step_name": steps.current_step(current).name
        if current and steps.current_step(current)
        else "",
        "main_action": taskpage.main_action(current, workflow.available_actions(current, user))
        if current and not preview
        else None,
        "sent_back": sent_back,
        "notice": notice,
        "status_labels": STATUS_LABELS,
        "pick_up": (
            board.preview_tasks(unclaimed[:PICK_UP_LIMIT])
            if preview
            else unclaimed[:PICK_UP_LIMIT]
        ),
        "preview": preview,
        "unclaimed_count": len(unclaimed),
        "notes_week": notes_week,
        "my_drafts": my_drafts,
        "in_review": in_review,
        "groups": groups,
        "total": progress.overall(groups),
    }
    if status is None:
        status = 503 if error else 200
    return templates.TemplateResponse(request, "home.html", context, status_code=status)
