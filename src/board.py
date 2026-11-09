"""The task board: /tasks lists tasks by subsystem, with a status filter."""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

import db
from auth import CurrentUser
from models import STATUS_LABELS, STATUSES, SUBSYSTEMS, Task

log = logging.getLogger("crush_coach")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()


def load_tasks(session: Session) -> list[Task]:
    return list(session.exec(select(Task).order_by(Task.code)).all())


def group_by_subsystem(tasks: list[Task]) -> list[tuple[str, list[Task]]]:
    """Every subsystem, in display order, including those with no tasks."""
    return [(name, [t for t in tasks if t.subsystem == name]) for name in SUBSYSTEMS]


def subsystem_progress(tasks: list[Task]) -> dict[str, dict[str, int]]:
    """Done and total task counts per subsystem, over every task (not the filtered view)."""
    return {
        name: {
            "done": sum(1 for t in tasks if t.subsystem == name and t.status == "done"),
            "total": sum(1 for t in tasks if t.subsystem == name),
        }
        for name in SUBSYSTEMS
    }


@router.get("/tasks", response_class=HTMLResponse)
def task_board(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    status: str = "",
):
    notice = None
    if status and status not in STATUSES:
        notice = f"Unknown status {status!r}; showing all tasks."
        status = ""

    error = None
    tasks: list[Task] = []
    try:
        tasks = load_tasks(session)
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        # Log the error type only: the message can contain the connection URL.
        log.error("Could not load tasks (%s)", type(exc).__name__)
        error = (
            "Couldn't load tasks from the database right now. Try again in a moment."
        )

    shown = [t for t in tasks if not status or t.status == status]
    context = {
        "groups": group_by_subsystem(shown),
        "progress": subsystem_progress(tasks),
        "status": status,
        "status_labels": STATUS_LABELS,
        "shown_count": len(shown),
        "total_count": len(tasks),
        "notice": notice,
        "error": error,
        "user": user,
    }
    return templates.TemplateResponse(
        request, "tasks.html", context, status_code=503 if error else 200
    )
