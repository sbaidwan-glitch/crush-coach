"""All tasks (`/tasks`): where is the team?

Filters (All / Mine / Unclaimed / Needs review), the counts box, one panel per part of
Crush with its progress ruler and task rows, recently recorded answers, and the "Suggest
a task" form. Leads act on work to check from Home, not here (`inbox.py`). Every number
comes from `Task.status` through `progress.py`.
"""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, col, select

import auth
import db
import profiles
import progress
import shell
from auth import MaybeUser
from models import (
    STATUS_LABELS,
    Task,
    TaskEvent,
    TaskSuggestion,
    utc_now,
)

log = logging.getLogger("crush_coach")
templates = shell.templates()
router = APIRouter()

# filter key -> label, in the order the buttons show
FILTERS = {
    "all": "All",
    "mine": "Mine",
    "unclaimed": "Unclaimed",
    "review": "Needs review",
}


# Made-up titles of different lengths. A signed-out visitor sees these (blurred) instead of
# the team's real task titles, so nothing real is in the page source.
PREVIEW_TITLES = (
    "Placeholder task title hidden until you sign in",
    "Hidden task title",
    "Another hidden task title for the preview",
    "Task title hidden",
    "A longer hidden task title that wraps onto a line",
)


def preview_task(task: Task, index: int) -> Task:
    """A copy of a task for a signed-out visitor: code, part and status only.

    Title and owner are made up, and the description and other tracker text are dropped.
    The copy is never saved.
    """
    return Task(
        code=task.code,
        title=PREVIEW_TITLES[index % len(PREVIEW_TITLES)],
        subsystem=task.subsystem,
        task_type=task.task_type,
        stage=task.stage,
        status=task.status,
        owner="Name hidden" if task.owner else None,
    )


def preview_tasks(tasks: list[Task]) -> list[Task]:
    return [preview_task(t, i) for i, t in enumerate(tasks)]


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


def default_filter(tasks: list[Task], user: dict | None) -> str:
    """Mine for a member who has tasks; All for leads, visitors and members with none yet
    (an empty page is no help to someone who hasn't claimed anything)."""
    if (
        user
        and user.get("role") != "lead"
        and any(progress.owned_by(t, user["name"], user["email"]) for t in tasks)
    ):
        return "mine"
    return "all"


def my_suggestions(session: Session, user: dict | None) -> list[TaskSuggestion]:
    """The signed-in member's own task suggestions, newest first, whatever became of them."""
    if not user:
        return []
    return list(
        session.exec(
            select(TaskSuggestion)
            .where(TaskSuggestion.suggested_by == user["name"])
            .order_by(col(TaskSuggestion.created_at).desc(), col(TaskSuggestion.id).desc())
        ).all()
    )


def age_text(since: datetime | None) -> str | None:
    """"under a day", "1 day", "3 days": how long a task has waited."""
    if since is None:
        return None
    days = max(0, (utc_now() - since).days)
    return "under a day" if days == 0 else f"{days} day{'' if days == 1 else 's'}"


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
    user: MaybeUser,
    show: str | None = None,
    suggested: bool = False,
):
    return render_board(
        request,
        session,
        user,
        show=show,
        notice="Sent to the leads. Thank you." if suggested and user else None,
    )


def render_board(
    request: Request,
    session: Session,
    user: auth.User | None,
    *,
    show: str | None = None,
    notice: str | None = None,
    status: int | None = None,
    draft: tuple[str, str] = ("", ""),
) -> HTMLResponse:
    """All tasks. `notice` is a plain sentence at the top (a filter we didn't know, a
    suggestion sent, why one couldn't be); `draft` refills the Suggest a task form.

    `show` is the filter. With none given, a member (not a lead, not a signed-out visitor)
    who owns at least one task starts on Mine; everyone else on All. "All" is always
    `?show=all`, so it is one click from Mine."""
    preview = user is None
    # Signed out, there is no "Mine".
    filters = {k: v for k, v in FILTERS.items() if not (preview and k == "mine")}
    if show is not None and show not in filters:
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

    if show is None:
        show = default_filter(tasks, user)
    suggestions: list[TaskSuggestion] = []
    try:
        suggestions = my_suggestions(session, user)
    except Exception as exc:  # noqa: BLE001  (the rest of the page still works)
        log.error("Could not load suggestions (%s)", type(exc).__name__)
    shown = apply_filter(tasks, show, user or {})
    # Rulers always count every task; the filter only changes which rows are listed.
    groups = progress.group_progress(tasks)
    visible = {t.id for t in shown}
    panels = [
        (g, [t for t in g.tasks if t.id in visible])
        for g in groups
        if g.total and (show == "all" or any(t.id in visible for t in g.tasks))
    ]
    if preview:
        # Counts and rulers above came from the real tasks; only the rows are replaced.
        panels = [(g, preview_tasks(rows)) for g, rows in panels]

    context = {
        "panels": panels,
        "left": panels[0::2],
        "right": panels[1::2],
        "counts": progress.counts(tasks),
        "filters": filters,
        "show": show,
        "shown_count": len(shown),
        "total_count": len(tasks),
        "status_labels": STATUS_LABELS,
        "notice": notice,
        "draft": draft,
        "error": error,
        "user": user,
        "preview": preview,
        # Signed out, owners are made up and get no chips (nothing to look up).
        "mine": (
            (lambda t: progress.owned_by(t, user["name"], user["email"]))
            if user
            else (lambda t: False)
        ),
        "suggestions": suggestions,
        "owner_chips": profiles.load(session).chips if user else (lambda owner: []),
    }
    if status is None:
        status = 503 if error else 200
    return templates.TemplateResponse(request, "tasks.html", context, status_code=status)
