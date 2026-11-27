"""Record (`/record`): what the team has settled, by part of Crush, newest first.

Answers (recorded by a lead from the Inbox: `inbox.py`) and, behind a "Show work notes"
toggle, members' notebook entries (`notebook.py`), each with whether it reached the Crush document.
Nothing here changes data except the Retry write button on an answer. The old `/decisions` addresses still
work: the coach's saved answers link to `/decisions/12`, which lands on that entry.
"""

import logging
import re
from datetime import date
from itertools import groupby
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

import db
import decisions
import drive_write
import notebook
import progress
import shell
from auth import CurrentUser, MaybeUser, locked_page

log = logging.getLogger("crush_coach")
templates = shell.templates()
router = APIRouter()

PARTS = {g["slug"]: g for g in progress.SUBSYSTEM_GROUPS}
MAX_QUERY_CHARS = 100


def _by_day(entries: list[dict[str, Any]]) -> list[tuple[date, list[dict[str, Any]]]]:
    """Entries (newest first) grouped by the day they were recorded."""
    return [(day, list(rows)) for day, rows in groupby(entries, key=lambda e: e["date"])]


def _add_answer_writes(session: Session, entries: list[dict[str, Any]]) -> None:
    """Say on each answer whether it reached the Crush document."""
    writes = notebook.writes_for(session, "answer", [e["id"] for e in entries])
    for entry in entries:
        entry["write"] = notebook.write_state(writes.get(entry["id"]), entry["subsystem"])


@router.get("/record", response_class=HTMLResponse)
def record_page(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: MaybeUser,
    part: str = "",
    q: str = "",
    notes: str = "",
):
    if user is None:  # signed out: nothing is read
        return locked_page(request, "the team’s Record")
    notice = None
    if part and part not in PARTS:
        notice = f"Unknown part {part!r}; showing {progress.SUBSYSTEM_GROUPS[0]['label']}."
    spec = PARTS.get(part, progress.SUBSYSTEM_GROUPS[0])
    q = " ".join(q.split())[:MAX_QUERY_CHARS]

    error = None
    show_notes = notes == "1"
    days: list[tuple[date, list[dict[str, Any]]]] = []
    notes_exist = False
    try:
        entries = decisions.recorded_entries(session, spec["subsystems"], q)
        _add_answer_writes(session, entries)
        notes_exist = notebook.has_notes(session, spec["subsystems"])
        if show_notes and notes_exist:
            entries += notebook.note_entries(session, spec["subsystems"], q)
            entries.sort(key=lambda e: e["when"], reverse=True)
        days = _by_day(entries)
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
        # The toggle is shown once a note exists for this part of Crush.
        "has_notes": notes_exist,
        "show_notes": show_notes and notes_exist,
        "doc_link": drive_write.doc_link(),
        "is_lead": user["role"] == "lead",
    }
    return templates.TemplateResponse(
        request, "record.html", context, status_code=503 if error else 200
    )


@router.post("/record/answers/{decision_id}/retry", response_class=Response)
def retry_answer_write(
    decision_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    part: Annotated[str, Form()] = "",
):
    """Retry write to Drive, for a recorded answer. Leads only (they record answers)."""
    if user["role"] != "lead":
        raise HTTPException(status_code=403)
    try:
        drive_write.retry(session, "answer", decision_id)
    except Exception as exc:  # noqa: BLE001  (the Record page says what happened)
        session.rollback()
        log.error("Could not retry an answer's write (%s)", type(exc).__name__)
    slug = part if re.fullmatch(r"[a-z]+", part or "") else ""
    return RedirectResponse(
        f"/record?part={slug}#answer-{decision_id}" if slug else "/record", status_code=303
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
