"""What every page's shell (the sidebar in base.html) needs, whatever the page.

Today that is one number: how many things wait on a lead (work to check, answers to
settle, suggested tasks; `inbox.count`), shown on the Inbox link for leads, and which task
the work log panel opens on. `templates()` makes a Jinja2Templates that adds it to every render, so
no page has to remember to pass it. Nothing here calls the model.
"""

import logging
import re
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

import auth
import db
import static_version

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


_TASK_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,19}")


def _code(value: object) -> str:
    """A task code from the address, or "" if it isn't shaped like one."""
    return value if isinstance(value, str) and _TASK_CODE.fullmatch(value) else ""


def _context(request: Request) -> dict:
    """`worklog_task`: the task whose page this is (the work log opens on it).
    `worklog_open`: a task code in `?log=`, which opens the work log on load (after "Mark
    step done")."""
    on_task = request.url.path.startswith("/tasks/")
    return {
        "needs_you": needs_you_count(request),
        "worklog_task": _code(request.path_params.get("code")) if on_task else "",
        "worklog_open": _code(request.query_params.get("log")),
    }


def templates() -> Jinja2Templates:
    return static_version.add_to(
        Jinja2Templates(
            directory=Path(__file__).parent / "templates", context_processors=[_context]
        )
    )
