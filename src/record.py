"""Record (`/record`): what the team has settled, by part of Crush, newest first.

Read-only. Answers only for now (recorded by a lead from Home: `inbox.py`); work notes
get a "Show work notes" toggle when they exist. The old `/decisions` addresses still
work: the coach's saved answers link to `/decisions/12`, which lands on that entry.
"""

import logging
from datetime import date
from itertools import groupby
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

import db
import decisions
import progress
import shell
from auth import MaybeUser, locked_page

log = logging.getLogger("crush_coach")
templates = shell.templates()
router = APIRouter()

PARTS = {g["slug"]: g for g in progress.SUBSYSTEM_GROUPS}
MAX_QUERY_CHARS = 100


def _by_day(entries: list[dict[str, Any]]) -> list[tuple[date, list[dict[str, Any]]]]:
    """Entries (newest first) grouped by the day they were recorded."""
    return [(day, list(rows)) for day, rows in groupby(entries, key=lambda e: e["date"])]


@router.get("/record", response_class=HTMLResponse)
def record_page(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: MaybeUser,
    part: str = "",
    q: str = "",
):
    if user is None:  # signed out: nothing is read
        return locked_page(request, "the team’s Record")
    notice = None
    if part and part not in PARTS:
        notice = f"Unknown part {part!r}; showing {progress.SUBSYSTEM_GROUPS[0]['label']}."
    spec = PARTS.get(part, progress.SUBSYSTEM_GROUPS[0])
    q = " ".join(q.split())[:MAX_QUERY_CHARS]

    error = None
    days: list[tuple[date, list[dict[str, Any]]]] = []
    try:
        days = _by_day(decisions.recorded_entries(session, spec["subsystems"], q))
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        session.rollback()
        log.error("Could not load the Record (%s)", type(exc).__name__)
        error = "Couldn't load the Record from the database right now. Try again in a moment."

    context = {
        "user": user,
        "parts": [(g["slug"], g["label"]) for g in progress.SUBSYSTEM_GROUPS],
        "part": spec["slug"],
        "label": spec["label"],
        "q": q,
        "days": days,
        "notice": notice,
        "error": error,
        # Work notes arrive with the work log; until some exist the toggle isn't shown.
        "has_notes": False,
    }
    return templates.TemplateResponse(
        request, "record.html", context, status_code=503 if error else 200
    )


@router.get("/decisions", response_class=Response)
def old_decisions_list() -> RedirectResponse:
    return RedirectResponse("/record", status_code=307)


@router.get("/decisions/{decision_id}", response_class=Response)
def old_decision_link(
    decision_id: str, session: Annotated[Session, Depends(db.get_session)]
) -> RedirectResponse:
    """/decisions/12 (saved in coach answers) to that answer's place in the Record."""
    found = None
    if decision_id.isdigit():
        try:
            found = decisions.decision_location(session, int(decision_id))
        except Exception as exc:  # noqa: BLE001  (fall back to the Record's front page)
            session.rollback()
            log.error("Could not find an old decision link (%s)", type(exc).__name__)
    if found is None:
        return RedirectResponse("/record", status_code=307)
    subsystem, found_id = found
    slug = next(
        (g["slug"] for g in progress.SUBSYSTEM_GROUPS if subsystem in g["subsystems"]), ""
    )
    return RedirectResponse(f"/record?part={slug}#answer-{found_id}", status_code=307)
