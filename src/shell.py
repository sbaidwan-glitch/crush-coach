"""What every page's shell (the sidebar in base.html) needs, whatever the page.

Today that is two numbers on the sidebar: how many things wait on a lead (tasks a member
notified a lead about, answers to settle, suggested tasks; `inbox.count`), shown on the Inbox link for leads, and
how many tasks are open (`open_task_count`, on the Tasks link); which task the Notebook
panel opens on; and the Notebook nudge (`notebook_nudge`: the task to write up, if any). `templates()` makes a Jinja2Templates that adds it to every render, so
no page has to remember to pass it. Nothing here calls the model.
"""

import logging
import re
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

import auth
import db
import localtime
import static_version
from models import Task

log = logging.getLogger("crush_coach")


def needs_you_count(request: Request) -> int | None:
    """What is waiting on a lead, if the signed-in person is a lead, else None.

    A failure (no session, database down) is None too: the badge just isn't shown and
    the page it sits on still works.
    """
    try:
        user = auth.session_user(request)
        if user is None or user["role"] != "lead":
            return None
        import inbox  # here, not at the top: inbox imports pages that import this module

        with Session(db.engine) as session:
            return inbox.count(session)
    except Exception as exc:  # noqa: BLE001  (the sidebar must never break a page)
        log.error("Could not count what is waiting on a lead (%s)", type(exc).__name__)
        return None


def open_task_count() -> int | None:
    """How many tasks are not done yet (the count on the sidebar's Tasks link).

    None if the database can't be read: the link then shows no count.
    """
    try:
        with Session(db.engine) as session:
            return session.exec(
                select(func.count()).select_from(Task).where(Task.status != "done")
            ).one()
    except Exception as exc:  # noqa: BLE001  (the sidebar must never break a page)
        log.error("Could not count the open tasks (%s)", type(exc).__name__)
        return None


def notebook_nudge(request: Request) -> dict[str, str] | None:
    """The task to nudge the member to write up (`notebook.banner`), or None.

    Only for a signed-in member on a full page: not the Notebook page itself, and not an
    htmx fragment (the page around it already has the nudge). Any failure is None, so the
    page just has no nudge.
    """
    try:
        user = auth.session_user(request)
        if user is None or request.headers.get("hx-request"):
            return None
        if request.url.path.startswith(("/notebook", "/worklog")):
            return None
        import notebook  # here, not at the top: notebook imports this module

        with Session(db.engine) as session:
            found = notebook.banner(session, request, user)
        return (
            {**found, "email": user["email"], "day": localtime.today().isoformat()}
            if found
            else None
        )
    except Exception as exc:  # noqa: BLE001  (the nudge must never break a page)
        log.error("Could not check the Notebook nudge (%s)", type(exc).__name__)
        return None


def my_chip(request: Request):
    """The signed-in person's own chip (icon and short name), for the top bar.

    None when signed out or if anything fails: the top bar then shows the default icon
    and the page still works.
    """
    try:
        user = auth.session_user(request)
        if user is None:
            return None
        import profiles  # here, not at the top: profiles imports this module

        with Session(db.engine) as session:
            return profiles.load(session).mine(user)
    except Exception as exc:  # noqa: BLE001  (the top bar must never break a page)
        log.error("Could not load the member's icon (%s)", type(exc).__name__)
        return None


_TASK_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,19}")


def _code(value: object) -> str:
    """A task code from the address, or "" if it isn't shaped like one."""
    return value if isinstance(value, str) and _TASK_CODE.fullmatch(value) else ""


def take_toast(request: Request) -> dict[str, str] | None:
    """The one-time confirmation `workflow.change_status` left in the session ("M06 moved
    to ..."), or None. Taken (and so gone) only by a full page, never by an htmx fragment
    or a request that has no session."""
    try:
        if request.headers.get("hx-request") or "session" not in request.scope:
            return None
        found = request.session.pop("toast", None)
    except Exception:  # noqa: BLE001  (a toast must never break a page)
        return None
    if isinstance(found, dict) and isinstance(found.get("text"), str):
        return {"text": found["text"], "code": _code(found.get("code"))}
    return None


def _context(request: Request) -> dict:
    """`notebook_task`: the task whose page this is (the Notebook opens on it).
    `toast`: the confirmation after "Move to next stage", shown once."""
    on_task = request.url.path.startswith("/tasks/")
    return {
        "needs_you": needs_you_count(request),
        "open_tasks": open_task_count(),
        "notebook_nudge": notebook_nudge(request),
        "my_chip": my_chip(request),
        "notebook_task": _code(request.path_params.get("code")) if on_task else "",
        "toast": take_toast(request),
    }


def templates() -> Jinja2Templates:
    return static_version.add_to(
        Jinja2Templates(
            directory=Path(__file__).parent / "templates", context_processors=[_context]
        )
    )
