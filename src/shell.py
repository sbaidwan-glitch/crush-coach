"""What every page's shell (the sidebar in base.html) needs, whatever the page.

Today that is one number: how many things wait on a lead (work to check, answers to
settle, suggested tasks; `inbox.count`), shown on the Inbox link for leads. `templates()` makes a Jinja2Templates that adds it to every render, so
no page has to remember to pass it. Nothing here calls the model.
"""

import logging
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

import auth
import db

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


def _context(request: Request) -> dict:
    return {"needs_you": needs_you_count(request)}


def templates() -> Jinja2Templates:
    return Jinja2Templates(
        directory=Path(__file__).parent / "templates", context_processors=[_context]
    )
