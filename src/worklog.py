"""The work log: a member's notebook of what they did on a task, and why.

A side panel with two tabs, **Log today's work** and **My notebook**, opened from the top
bar, from the Home banner (shown when the member was active today and hasn't logged;
"Not today" hides it until tomorrow) and after "Mark step done". The panel is a fragment
(`/worklog/panel`, loaded into a dialog by `worklog.js`); `/worklog` is the same content as
a whole page, so everything works without JavaScript too.

How a note is made:

1. The member writes **what they did** and **why** (recommended).
2. **Tidy my note** (the only model call here, on the button): `llm.tidy_note` rewrites it
   for clarity, adding specifics only from the member's text, their coach chat on the task or
   a linked source. If a detail is missing it returns one question, shown in the ask-back box.
   Nothing is saved.
3. The member edits the result and confirms with **Post to the Crush document**. Only then is
   a `WorkNote` saved and appended to the Crush document (`drive_write.py`). No lead approves it.

A note that couldn't be written to the document stays in the notebook with "Retry write to
Drive". Notes also show in the task's History and in the Record (as a Note, behind its toggle).
"""

import logging
import os
import re
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, col, func, select

import coach
import db
import drive_write
import llm
import localtime
import shell
import workflow
from auth import CurrentUser, User
from models import (
    CoachMessage,
    DriveChunk,
    DriveFile,
    DriveWrite,
    LlmCall,
    Task,
    TaskEvent,
    WorkNote,
    utc_now,
)
from progress import owned_by

log = logging.getLogger("crush_coach")
templates = shell.templates()
router = APIRouter()

DEFAULT_TIDY_LIMIT = 20  # Tidy my note, per member per day (TIDY_DAILY_LIMIT)
MAX_FIELD_CHARS = llm.MAX_NOTE_CHARS
MAX_REPLY_CHARS = 300
NOTEBOOK_LIMIT = 50
TASK_OPTIONS_LIMIT = 40
DUPLICATE_WINDOW = timedelta(minutes=2)  # a double click posts one note
DB_ERROR = "Couldn't reach the database right now. Nothing was saved. Try again."
TABS = ("log", "notebook")
_TASK_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,19}")
_GOOGLE_LINK = re.compile(r"https://(?:docs|drive)\.google\.com/[^\s<>)\]]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STATUS_RANK = {"in_progress": 0, "needs_review": 1, "todo": 2, "done": 3}


class LogError(Exception):
    """Something the member can fix or must be told. The message is safe to show."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def tidy_limit() -> int:
    try:
        return max(0, int(os.environ.get("TIDY_DAILY_LIMIT") or DEFAULT_TIDY_LIMIT))
    except ValueError:
        return DEFAULT_TIDY_LIMIT


def clean(value: str, limit: int = MAX_FIELD_CHARS) -> str:
    """What a member typed, tidied for saving: control characters out, ends trimmed."""
    return _CONTROL.sub("", (value or "").replace("\r\n", "\n")).strip()[:limit]


def valid_code(value: str | None) -> str:
    """A task code from a query string or form, or "" if it isn't shaped like one."""
    value = (value or "").strip()
    return value if _TASK_CODE.fullmatch(value) else ""


# --- which task, and the Home banner ---------------------------------------------------


def my_tasks(session: Session, user: User) -> list[Task]:
    """Tasks the member owns, the ones in progress first."""
    owned = [
        t
        for t in session.exec(select(Task).order_by(Task.code)).all()
        if owned_by(t, user["name"], user["email"])
    ]
    owned.sort(key=lambda t: (_STATUS_RANK.get(t.status, 9), t.code))
    return owned[:TASK_OPTIONS_LIMIT]


def active_task_today(session: Session, user: User) -> Task | None:
    """The task the member most recently worked on today (moved it, or asked its coach
    something) that they haven't logged today; None if there isn't one."""
    start = localtime.day_start_utc()
    moves = session.exec(
        select(TaskEvent.task_id, TaskEvent.created_at).where(
            TaskEvent.actor == user["name"], TaskEvent.created_at >= start
        )
    ).all()
    asked = session.exec(
        select(CoachMessage.task_id, CoachMessage.created_at).where(
            CoachMessage.role == "user",
            func.lower(CoachMessage.member) == user["email"].casefold(),
            CoachMessage.created_at >= start,
        )
    ).all()
    latest: dict[int, Any] = {}
    for task_id, when in [*moves, *asked]:
        if task_id is not None and (task_id not in latest or when > latest[task_id]):
            latest[task_id] = when
    logged = set(
        session.exec(
            select(WorkNote.task_id).where(
                func.lower(WorkNote.member) == user["email"].casefold(),
                WorkNote.posted_at >= start,
            )
        ).all()
    )
    for task_id in sorted(latest, key=latest.__getitem__, reverse=True):
        if task_id not in logged:
            return session.get(Task, task_id)
    return None


def banner(session: Session, request: Request, user: User) -> dict[str, str] | None:
    """The Home banner: the task the member worked on today and hasn't logged. None once
    they said "Not today" (until tomorrow) or when there is nothing to log."""
    if request.session.get("worklog_dismissed") == localtime.today().isoformat():
        return None
    task = active_task_today(session, user)
    return {"code": task.code, "title": task.title} if task else None


def task_choices(
    session: Session, user: User, code: str
) -> tuple[list[Task], Task | None]:
    """The tasks to pick from, and the one selected: the one asked for, else the one
    worked on today, else the first the member owns."""
    options = my_tasks(session, user)
    chosen = next((t for t in options if t.code == code), None) if code else None
    if chosen is None and code:
        chosen = session.exec(select(Task).where(Task.code == code)).first()
        if chosen is not None:
            options.append(chosen)
    if chosen is None:
        chosen = active_task_today(session, user) or (options[0] if options else None)
        if chosen is not None and chosen not in options:
            options.append(chosen)
    return options, chosen


# --- the notebook ------------------------------------------------------------------------


def write_state(row: DriveWrite | None, subsystem: str) -> dict[str, Any]:
    """Where an entry stands in the Crush document, for a page to show."""
    status = row.status if row else "pending"
    return {
        "status": "written" if status == "written" else "failed" if status == "failed" else "pending",
        "text": drive_write.status_text(row, subsystem),
        "link": drive_write.doc_link(row.tab_id) if row and status == "written" else None,
    }


def note_view(note: WorkNote, write: DriveWrite | None) -> dict[str, Any]:
    return {
        "id": note.id,
        "task_code": note.task_code,
        "did": note.did,
        "why": note.why,
        "who": note.member_name,
        "when": note.posted_at,
        "date": localtime.local_date(note.posted_at),
        "write": write_state(write, note.subsystem),
    }


def writes_for(session: Session, kind: str, ids: list[int]) -> dict[int, DriveWrite]:
    if not ids:
        return {}
    rows = session.exec(
        select(DriveWrite).where(DriveWrite.kind == kind, col(DriveWrite.ref_id).in_(ids))
    ).all()
    return {row.ref_id: row for row in rows}


def notebook(session: Session, user: User) -> list[tuple[date, list[dict[str, Any]]]]:
    """The member's own notes, newest first, grouped by the day they were posted."""
    notes = session.exec(
        select(WorkNote)
        .where(func.lower(WorkNote.member) == user["email"].casefold())
        .order_by(col(WorkNote.posted_at).desc(), col(WorkNote.id).desc())
        .limit(NOTEBOOK_LIMIT)
    ).all()
    writes = writes_for(session, "note", [n.id for n in notes])
    days: list[tuple[date, list[dict[str, Any]]]] = []
    for note in notes:
        view = note_view(note, writes.get(note.id))
        if not days or days[-1][0] != view["date"]:
            days.append((view["date"], []))
        days[-1][1].append(view)
    return days


def note_entries(
    session: Session, subsystems: tuple[str, ...], query: str = ""
) -> list[dict[str, Any]]:
    """Notes for some subsystems as Record entries (newest first). `query` keeps the ones
    that contain every word."""
    notes = session.exec(
        select(WorkNote)
        .where(col(WorkNote.subsystem).in_(subsystems))
        .order_by(col(WorkNote.posted_at).desc(), col(WorkNote.id).desc())
    ).all()
    words = query.casefold().split()
    writes = writes_for(session, "note", [n.id for n in notes])
    entries = []
    for note in notes:
        haystack = f"{note.member_name} {note.task_code} {note.did} {note.why}".casefold()
        if all(word in haystack for word in words):
            entries.append({"kind": "note", **note_view(note, writes.get(note.id))})
    return entries


def has_notes(session: Session, subsystems: tuple[str, ...]) -> bool:
    return (
        session.exec(
            select(func.count())
            .select_from(WorkNote)
            .where(col(WorkNote.subsystem).in_(subsystems))
        ).one()
        > 0
    )


# --- tidying and posting -------------------------------------------------------------------


def tidies_today(session: Session, email: str) -> int:
    """How many tidies the member has had today: model calls to the tidy model that worked.
    (`llm_call` has no purpose column, so keep `TIDY_MODEL` different from `COACH_MODEL`,
    or coach answers count here too.)"""
    return session.exec(
        select(func.count())
        .select_from(LlmCall)
        .where(
            func.lower(LlmCall.member) == email.casefold(),
            LlmCall.model == llm.tidy_model(),
            LlmCall.status == "ok",
            LlmCall.created_at >= localtime.day_start_utc(),
        )
    ).one()


def linked_sources(
    session: Session, chat: list[CoachMessage], text: str
) -> list[dict[str, str]]:
    """Sources the model may take specifics from: ones the coach cited in this task's chat
    and Google links in the member's own words, each with what the local Drive index holds
    of it (no Google call)."""
    found = [dict(s) for s in coach._earlier_sources(chat)]
    known = {s.get("link") for s in found}
    found.extend({"title": "", "link": link} for link in dict.fromkeys(_GOOGLE_LINK.findall(text)) if link not in known)
    sources = []
    for source in found[: llm.MAX_TIDY_SOURCES]:
        body = ""
        file_id = llm.drive_file_id(str(source.get("link") or ""))
        indexed = (
            session.exec(select(DriveFile).where(DriveFile.drive_id == file_id)).first()
            if file_id
            else None
        )
        if indexed is not None:
            chunks = session.exec(
                select(DriveChunk.text)
                .where(DriveChunk.file_id == indexed.id)
                .order_by(DriveChunk.position)
                .limit(3)
            ).all()
            body = "\n".join(chunks)[: llm.MAX_TIDY_SOURCE_CHARS]
        sources.append(
            {
                "title": str(source.get("title") or (indexed.name if indexed else "")),
                "link": str(source.get("link") or ""),
                "text": body,
            }
        )
    return sources


def tidy(
    session: Session, user: User, code: str, did: str, why: str, asked: str = "", reply: str = ""
) -> llm.TidyResult:
    """Tidy the member's note: one model call, logged, nothing saved. Raises LogError."""
    task = session.exec(select(Task).where(Task.code == code)).first()
    if task is None:
        raise LogError("Choose which task this is about.")
    did, why = clean(did), clean(why)
    reply = clean(reply, MAX_REPLY_CHARS)
    if not did:
        raise LogError("Write what you did first, then tidy it.")
    if tidies_today(session, user["email"]) >= tidy_limit():
        raise LogError(
            f"You've used today's {tidy_limit()} tidies. You can still post your note as written."
        )
    thread = coach._chat(coach._history(session, task.id))
    chat = [{"role": m.role, "content": m.content} for m in thread]
    sources = linked_sources(session, thread, f"{did}\n{why}\n{reply}")
    calls: list[llm.CallRecord] = []
    error: llm.LLMError | None = None
    result: llm.TidyResult | None = None
    try:
        result = llm.tidy_note(
            coach.task_context(task), did, why, chat, sources,
            asked=clean(asked, llm.MAX_TIDY_QUESTION_CHARS), reply=reply, record=calls.append,
        )
    except llm.LLMError as exc:
        error = exc
    except Exception as exc:  # noqa: BLE001  (never let the model break the panel)
        log.error("Tidying a note failed unexpectedly (%s)", type(exc).__name__)
        error = llm.LLMError("error", "The note couldn't be tidied. Try again.")
    try:  # the cost of every tidy, failed or not (the cost-benefit analysis reads this)
        coach._add_llm_calls(session, task.id, user["email"], calls)
        session.commit()
    except Exception as exc:  # noqa: BLE001  (the member still gets their tidied note)
        session.rollback()
        log.error("Could not log a tidy (%s)", type(exc).__name__)
    if result is None:
        raise LogError(f"{error.message} You can still post your note as written.", 502)
    return result


def post_note(
    session: Session,
    user: User,
    code: str,
    did: str,
    why: str,
    original_did: str = "",
    original_why: str = "",
) -> WorkNote:
    """Save the note and write it to the Crush document. Raises LogError if it can't be
    saved. The write itself can fail without losing the note (`DriveWrite` says so).
    Posting the same note twice in a row returns the first."""
    task = session.exec(select(Task).where(Task.code == code)).first()
    if task is None:
        raise LogError("Choose which task this is about.")
    did, why = clean(did), clean(why)
    if not did:
        raise LogError("Write what you did before posting.")
    original_did, original_why = clean(original_did), clean(original_why)
    if original_did:  # the member's own words from before they used Tidy
        original = original_did + (f"\n\nWhy: {original_why}" if original_why else "")
    else:
        original = did + (f"\n\nWhy: {why}" if why else "")
    now = utc_now()
    note = session.exec(
        select(WorkNote).where(
            func.lower(WorkNote.member) == user["email"].casefold(),
            WorkNote.task_id == task.id,
            WorkNote.did == did,
            WorkNote.why == why,
            WorkNote.posted_at >= now - DUPLICATE_WINDOW,
        )
    ).first()
    if note is None:
        note = WorkNote(
            member=user["email"],
            member_name=user["name"],
            task_id=task.id,
            task_code=task.code,
            subsystem=task.subsystem,
            did=did,
            why=why,
            original=original,
            posted_at=now,
        )
        session.add(note)
        session.commit()
        session.refresh(note)
    drive_write.write_note(session, note)  # after the commit; never raises
    return note


# --- the panel ----------------------------------------------------------------------------


def panel_context(
    session: Session,
    user: User,
    *,
    tab: str = "log",
    code: str = "",
    form: dict[str, Any] | None = None,
    error: str | None = None,
    notice: str | None = None,
    posted: dict[str, Any] | None = None,
    status: int = 200,
) -> dict[str, Any]:
    """Everything `_worklog_panel.html` shows. A failure loading one tab costs only that tab."""
    tab = tab if tab in TABS else "log"
    context: dict[str, Any] = {
        "user": user,
        "tab": tab,
        "error": error,
        "notice": notice,
        "posted": posted,
        "form": form or {},
        "tasks": [],
        "task": None,
        "days": [],
        "load_error": None,
        "tidy_limit": tidy_limit(),
        "doc_link": drive_write.doc_link(),
        "status": status,
    }
    try:
        if tab == "log":
            tasks, chosen = task_choices(session, user, code or (form or {}).get("task", ""))
            context.update(tasks=tasks, task=chosen)
        else:
            context["days"] = notebook(session, user)
    except Exception as exc:  # noqa: BLE001  (a database failure must not break the page)
        session.rollback()
        log.error("Could not load the work log (%s)", type(exc).__name__)
        context["load_error"] = "Couldn't load this from the database right now. Try again in a moment."
    return context


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render_panel(request: Request, session: Session, user: User, **kwargs: Any) -> Response:
    """The panel as a fragment (htmx and `worklog.js`), or as a whole page otherwise."""
    context = panel_context(session, user, **kwargs)
    status = context["status"]
    if is_htmx(request):
        return templates.TemplateResponse(request, "_worklog_panel.html", context, status_code=status)
    return templates.TemplateResponse(request, "worklog.html", context, status_code=status)


@router.get("/worklog", response_class=HTMLResponse)
def worklog_page(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    tab: str = "log",
    task: str = "",
):
    return render_panel(request, session, user, tab=tab, code=valid_code(task))


@router.get("/worklog/panel", response_class=HTMLResponse)
def worklog_panel(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    tab: str = "log",
    task: str = "",
):
    context = panel_context(session, user, tab=tab, code=valid_code(task))
    return templates.TemplateResponse(
        request, "_worklog_panel.html", context, status_code=context["status"]
    )


@router.post("/worklog/tidy", response_class=HTMLResponse)
def tidy_my_note(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    task: Annotated[str, Form()] = "",
    did: Annotated[str, Form()] = "",
    why: Annotated[str, Form()] = "",
    original_did: Annotated[str, Form()] = "",
    original_why: Annotated[str, Form()] = "",
    asked: Annotated[str, Form()] = "",
    reply: Annotated[str, Form()] = "",
):
    """Tidy my note: the one model call. Shows the result for the member to edit; saves
    no note."""
    form = {
        "task": valid_code(task),
        "did": clean(did),
        "why": clean(why),
        # Their own words are kept from the first tidy, whatever they edit after it.
        "original_did": clean(original_did) or clean(did),
        "original_why": clean(original_why) if original_did else clean(why),
    }
    try:
        result = tidy(session, user, form["task"], did, why, asked, reply)
    except LogError as exc:
        session.rollback()
        return render_panel(request, session, user, form=form, error=exc.message, status=exc.status)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not tidy a note (%s)", type(exc).__name__)
        return render_panel(request, session, user, form=form, error=DB_ERROR, status=503)
    form.update(
        did=result.did,
        why=result.why,
        tidied=True,
        question=result.question,
        kept_original=result.kept_original,
    )
    return render_panel(request, session, user, form=form)


@router.post("/worklog/post", response_class=HTMLResponse)
def post_my_note(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    task: Annotated[str, Form()] = "",
    did: Annotated[str, Form()] = "",
    why: Annotated[str, Form()] = "",
    original_did: Annotated[str, Form()] = "",
    original_why: Annotated[str, Form()] = "",
):
    """Post: save the note, add it to the Crush document, show it in the notebook."""
    form = {
        "task": valid_code(task),
        "did": clean(did),
        "why": clean(why),
        "original_did": clean(original_did),
        "original_why": clean(original_why),
        "tidied": bool(original_did),
    }
    try:
        note = post_note(session, user, form["task"], did, why, original_did, original_why)
        write = session.exec(
            select(DriveWrite).where(DriveWrite.kind == "note", DriveWrite.ref_id == note.id)
        ).first()
        posted = {"id": note.id, "write": write_state(write, note.subsystem)}
    except LogError as exc:
        session.rollback()
        return render_panel(request, session, user, form=form, error=exc.message, status=exc.status)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not post a work note (%s)", type(exc).__name__)
        return render_panel(request, session, user, form=form, error=DB_ERROR, status=503)
    if not is_htmx(request):
        return RedirectResponse("/worklog?tab=notebook", status_code=303)
    return render_panel(request, session, user, tab="notebook", posted=posted)


def _retry_note(
    request: Request, session: Session, user: User, note_id: int
) -> Response:
    try:
        note = session.get(WorkNote, note_id)
        if note is None or (
            note.member.casefold() != user["email"].casefold() and user["role"] != "lead"
        ):
            raise LogError("That note isn't in your notebook.", 404)
        row = drive_write.retry(session, "note", note.id)
        posted = {"id": note.id, "write": write_state(row, note.subsystem)}
    except LogError as exc:
        session.rollback()
        return render_panel(request, session, user, tab="notebook", error=exc.message, status=exc.status)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not retry a note's write (%s)", type(exc).__name__)
        return render_panel(request, session, user, tab="notebook", error=DB_ERROR, status=503)
    if not is_htmx(request):
        return RedirectResponse("/worklog?tab=notebook", status_code=303)
    return render_panel(request, session, user, tab="notebook", posted=posted)


@router.post("/worklog/notes/{note_id}/retry", response_class=HTMLResponse)
def retry_note_write(
    request: Request,
    note_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
):
    """Retry write to Drive, for a note that couldn't be added to the Crush document."""
    return _retry_note(request, session, user, note_id)


@router.post("/worklog/answers/{decision_id}/retry", response_class=Response)
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


@router.post("/worklog/dismiss", response_class=Response)
def not_today(request: Request, user: CurrentUser, next: Annotated[str, Form()] = "/"):
    """"Not today" on the Home banner: hide it until tomorrow."""
    request.session["worklog_dismissed"] = localtime.today().isoformat()
    return RedirectResponse(workflow._safe_next(next, "/"), status_code=303)
