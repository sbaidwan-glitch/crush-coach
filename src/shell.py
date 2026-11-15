"""What every page's shell (the sidebar in base.html) needs, whatever the page.

Today that is one number: how many tasks wait for a lead's review, shown on the Tasks
link for leads. `templates()` makes a Jinja2Templates that adds it to every render, so
no page has to remember to pass it. Nothing here calls the model.
"""

import logging
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

import auth
import db
from models import Task

log = logging.getLogger("crush_coach")


def review_count(request: Request) -> int | None:
    """Tasks waiting for review if the signed-in person is a lead, else None.

    A failure (no session, database down) is None too: the badge just isn't shown and
    the page it sits on still works.
    """
    try:
        user = auth.session_user(request)
        if user is None or user["role"] != "lead":
            return None
        with Session(db.engine) as session:
            return session.exec(
                select(func.count()).select_from(Task).where(Task.status == "needs_review")
            ).one()
    except Exception as exc:  # noqa: BLE001  (the sidebar must never break a page)
        log.error("Could not count tasks waiting for review (%s)", type(exc).__name__)
        return None


def _context(request: Request) -> dict:
    return {"review_waiting": review_count(request)}


def templates() -> Jinja2Templates:
    return Jinja2Templates(
        directory=Path(__file__).parent / "templates", context_processors=[_context]
    )
