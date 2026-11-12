"""Team facts: short sentences every coach answer is told (`/admin/facts`, leads only).

Facts like "Current season is 2026-27" are why the coach never has to ask which season
or which vehicle. They live in the `team_fact` table (three are seeded by the
migration) and ALL of them go into every prompt, so they stay short.

The page is plain forms with redirect-after-post: add, edit and remove each post, then
land back on the list with a one-line result. Nothing here calls the model or Drive.
"""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

import db
from auth import LeadUser
from models import TeamFact, utc_now

log = logging.getLogger("crush_coach")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter(prefix="/admin/facts")

MAX_FACT_CHARS = 300
MAX_FACTS = 40  # every fact is in every prompt: keep the list readable and cheap

DB_ERROR = (
    "Couldn't reach the database right now. Nothing was saved. Try again in a moment."
)
# The result line after a post. The page shows one of these by key, never text from
# the URL, so a crafted link can't put words on the page.
RESULTS = {
    "added": "Fact added. The coach uses it from the next message.",
    "saved": "Fact saved. The coach uses it from the next message.",
    "removed": "Fact removed.",
    "missing": "That fact is already gone (someone else removed it).",
}


def clean(text: str) -> str:
    """One line, trimmed: facts go into the prompt as a list, one per line."""
    return " ".join(text.split())


def check(text: str) -> str | None:
    """A plain sentence about what is wrong with this fact, or None if it is fine."""
    if not text:
        return "Write the fact first. It can't be empty."
    if len(text) > MAX_FACT_CHARS:
        return (
            f"Keep each fact to {MAX_FACT_CHARS} characters or fewer. "
            "Split it into two facts if it needs more."
        )
    return None


def fact_texts(session: Session) -> list[str]:
    """Every fact, oldest first: what the coach is told."""
    rows = session.exec(select(TeamFact).order_by(TeamFact.id)).all()
    return [row.text for row in rows]


def _page(
    request: Request,
    session: Session,
    user,
    *,
    error: str | None = None,
    result: str | None = None,
    draft: str = "",
    status: int = 200,
) -> Response:
    facts: list[TeamFact] = []
    load_error: str | None = None
    try:
        facts = list(session.exec(select(TeamFact).order_by(TeamFact.id)).all())
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Could not load team facts (%s)", type(exc).__name__)
        load_error = "Couldn't load the team facts from the database right now."
        status = 503
    return templates.TemplateResponse(
        request,
        "admin_facts.html",
        {
            "user": user,
            "facts": facts,
            "load_error": load_error,
            "error": error,
            "result": RESULTS.get(result or ""),
            "draft": draft,
            "max_chars": MAX_FACT_CHARS,
        },
        status_code=status,
    )


def _done(result: str) -> Response:
    return RedirectResponse(f"/admin/facts?done={result}", status_code=303)


@router.get("", response_class=HTMLResponse)
def facts_page(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: LeadUser,
    done: str | None = None,
):
    return _page(request, session, user, result=done)


@router.post("")
def add_fact(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: LeadUser,
    text: Annotated[str, Form()] = "",
):
    text = clean(text)
    problem = check(text)
    try:
        if problem is None and len(session.exec(select(TeamFact)).all()) >= MAX_FACTS:
            problem = (
                f"There are already {MAX_FACTS} facts. Remove one before adding more."
            )
        if problem is not None:
            return _page(request, session, user, error=problem, draft=text, status=400)
        session.add(TeamFact(text=text, updated_by=user["email"]))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not add a team fact (%s)", type(exc).__name__)
        return _page(request, session, user, error=DB_ERROR, draft=text, status=503)
    return _done("added")


@router.post("/{fact_id}")
def edit_fact(
    request: Request,
    fact_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    user: LeadUser,
    text: Annotated[str, Form()] = "",
):
    text = clean(text)
    problem = check(text)
    if problem is not None:
        return _page(request, session, user, error=problem, status=400)
    try:
        fact = session.get(TeamFact, fact_id)
        if fact is None:
            return _done("missing")
        fact.text = text
        fact.updated_by = user["email"]
        fact.updated_at = utc_now()
        session.add(fact)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not save a team fact (%s)", type(exc).__name__)
        return _page(request, session, user, error=DB_ERROR, status=503)
    return _done("saved")


@router.post("/{fact_id}/delete")
def remove_fact(
    request: Request,
    fact_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    user: LeadUser,
):
    try:
        fact = session.get(TeamFact, fact_id)
        if fact is None:
            return _done("missing")
        session.delete(fact)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not remove a team fact (%s)", type(exc).__name__)
        return _page(request, session, user, error=DB_ERROR, status=503)
    return _done("removed")
