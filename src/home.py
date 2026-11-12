"""Home (`/`): what should I do next?

Your task (the stored "Next step" from the coach, never a fresh model call), a few
unclaimed tasks to pick up, Crush's build progress, and the render with a tag per part.
Signed-out visitors get a sign-in panel in the same shell.
"""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

import auth
import board
import db
import progress
import taskpage
from models import STATUS_LABELS, Task

log = logging.getLogger("crush_coach")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()

PICK_UP_LIMIT = 4


def my_task(tasks: list[Task], user: auth.User) -> tuple[Task | None, int]:
    """The member's current task, and how many more they have.

    A task in progress comes first; one waiting for review is shown if nothing else is.
    """
    mine = [t for t in tasks if progress.owned_by(t, user["name"], user["email"])]
    for status in ("in_progress", "needs_review"):
        current = [t for t in mine if t.status == status]
        if current:
            others = len([t for t in mine if t.status in progress.STARTED]) - 1
            return current[0], others
    return None, 0


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: Annotated[Session, Depends(db.get_session)]):
    user = auth.session_user(request)
    if user is None:
        return templates.TemplateResponse(request, "home_signed_out.html", {"user": None})

    error = None
    tasks: list[Task] = []
    try:
        tasks = board.load_tasks(session)
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Could not load tasks (%s)", type(exc).__name__)
        error = "Couldn't load tasks from the database right now. Try again in a moment."

    current, more = my_task(tasks, user)
    next_step = None
    if current is not None:
        try:
            next_step = taskpage.latest_next_steps(session, [current.id]).get(current.id)
        except Exception as exc:  # noqa: BLE001  (the rest of the page still works)
            session.rollback()
            log.error("Could not load the next step (%s)", type(exc).__name__)

    groups = progress.group_progress(tasks)
    unclaimed = [t for t in tasks if progress.is_unclaimed(t)]
    context = {
        "user": user,
        "error": error,
        "task": current,
        "more_tasks": more,
        "next_step": next_step,
        "stages": taskpage.stage_strip(current) if current else [],
        "status_labels": STATUS_LABELS,
        "pick_up": unclaimed[:PICK_UP_LIMIT],
        "unclaimed_count": len(unclaimed),
        "groups": groups,
        "total": progress.overall(groups),
    }
    return templates.TemplateResponse(
        request, "home.html", context, status_code=503 if error else 200
    )
