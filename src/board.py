"""All tasks (`/tasks`): where is the team?

Filters (All / Mine / Unclaimed / Needs review), the counts box, one panel per part of
Crush with its progress ruler and task rows, recent decisions, and (leads only) the tasks
waiting for review. Every number comes from `Task.status` through `progress.py`.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, select

import db
import progress
from auth import CurrentUser
from models import (
    DECISION_STATUS_LABELS,
    STATUS_LABELS,
    Decision,
    Task,
    TaskEvent,
    utc_now,
)

log = logging.getLogger("crush_coach")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()

# filter key -> label, in the order the buttons show
FILTERS = {
    "all": "All",
    "mine": "Mine",
    "unclaimed": "Unclaimed",
    "review": "Needs review",
}
RECENT_DECISIONS = 3


def load_tasks(session: Session) -> list[Task]:
    return list(session.exec(select(Task).order_by(Task.code)).all())


def apply_filter(tasks: list[Task], key: str, user: dict) -> list[Task]:
    if key == "mine":
        return [t for t in tasks if progress.owned_by(t, user["name"], user["email"])]
    if key == "unclaimed":
        return [t for t in tasks if progress.is_unclaimed(t)]
    if key == "review":
        return [t for t in tasks if t.status == "needs_review"]
    return tasks


def age_text(since: datetime | None) -> str | None:
    """"today", "1 day", "3 days": how long a task has waited."""
    if since is None:
        return None
    days = max(0, (utc_now() - since).days)
    return "today" if days == 0 else f"{days} day{'' if days == 1 else 's'}"


def waiting_for_review(session: Session, tasks: list[Task]) -> list[dict]:
    """Tasks waiting for a lead, longest wait first, with how long they've waited."""
    rows = []
    for task in tasks:
        if task.status != "needs_review":
            continue
        event = session.exec(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.to_status == "needs_review")
            .order_by(col(TaskEvent.created_at).desc())
        ).first()
        rows.append(
            {
                "task": task,
                "since": event.created_at if event else None,
            }
        )
    rows.sort(key=lambda r: (r["since"] is None, r["since"] or utc_now()))
    for row in rows:
        row["age"] = age_text(row["since"])
    return rows


@router.get("/tasks", response_class=HTMLResponse)
def task_board(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    show: str = "all",
):
    notice = None
    if show not in FILTERS:
        notice = f"Unknown filter {show!r}; showing all tasks."
        show = "all"

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

    shown = apply_filter(tasks, show, user)
    # Rulers always count every task; the filter only changes which rows are listed.
    groups = progress.group_progress(tasks)
    visible = {t.id for t in shown}
    panels = [
        (g, [t for t in g.tasks if t.id in visible])
        for g in groups
        if g.total and (show == "all" or any(t.id in visible for t in g.tasks))
    ]

    # Decisions and the review queue load on their own: a failure only hides that panel.
    recent: list[Decision] = []
    decisions_error = None
    try:
        recent = list(
            session.exec(
                select(Decision)
                .where(Decision.status != "superseded")
                .order_by(col(Decision.created_at).desc(), col(Decision.id).desc())
                .limit(RECENT_DECISIONS)
            ).all()
        )
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not load recent decisions (%s)", type(exc).__name__)
        decisions_error = "Couldn't load decisions right now."

    review: list[dict] = []
    review_error = None
    if user["role"] == "lead":
        try:
            review = waiting_for_review(session, tasks)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            log.error("Could not load the review queue (%s)", type(exc).__name__)
            review_error = "Couldn't load the review queue right now."

    context = {
        "panels": panels,
        "left": panels[0::2],
        "right": panels[1::2],
        "counts": progress.counts(tasks),
        "filters": FILTERS,
        "show": show,
        "shown_count": len(shown),
        "total_count": len(tasks),
        "status_labels": STATUS_LABELS,
        "recent_decisions": recent,
        "decision_status_labels": DECISION_STATUS_LABELS,
        "decisions_error": decisions_error,
        "review": review,
        "review_error": review_error,
        "notice": notice,
        "error": error,
        "user": user,
    }
    return templates.TemplateResponse(
        request, "tasks.html", context, status_code=503 if error else 200
    )
