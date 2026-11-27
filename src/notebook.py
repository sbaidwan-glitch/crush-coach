"""The Notebook: a member's own entries about what they did on a task, and why.

It replaces the work log. A side panel (`/notebook/panel`, loaded into a dialog by
`notebook.js`) and a whole page (`/notebook`, compose on the left and history on the right)
show the same three things, so everything also works without JavaScript:

- **Today's entry**: pick the task, add photos, write what you did and why, **Tidy my note**
  (one model call, on the button), **Suggest specs** (one model run, on the button), then
  **Save draft** or **Publish to Crush doc**.
- **Earlier**: every entry of the member's, 20 at a time, each with its state.
- **Private notes**: words only the member can read. No task, no Drive write, no model call.

An entry is a **Draft** (only its member sees it, and can edit it), **Published** (read-only
from then on: to correct one, the member adds a new entry that refers to it) or **Failed to
publish** (published, but the Crush document wouldn't take it; **Retry** writes it again).
The state is `WorkNote.state` plus what the entry's `DriveWrite` row says.

Publishing appends the entry, the specs the member accepted and a link to each photo to the
Crush document (`drive_write.py`, append-only, once), after uploading the photos to a
"Notebook photos" folder. A failed photo upload never stops the text. Nobody approves an
entry. A draft, its photos and its specs are served only to its member; published photos to
any signed-in member. Private notes are never read by any query that doesn't filter on the
signed-in member, and nothing in this module sends one to a model or to Google.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import defer
from sqlmodel import Session, col, delete, func, select

import coach
import db
import drive_write
import llm
import localtime
import photos
import retrieval
import shell
import specs
import workflow
from auth import CurrentUser, MaybeUser, User, locked_page
from models import (
    NOTE_STATE_LABELS,
    CoachMessage,
    DriveChunk,
    DriveFile,
    DriveWrite,
    LlmCall,
    NotePhoto,
    NoteSpec,
    PrivateNote,
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
DEFAULT_SPECS_LIMIT = 10  # Suggest specs, per member per day (SPECS_DAILY_LIMIT)
MAX_FIELD_CHARS = llm.MAX_NOTE_CHARS
MAX_REPLY_CHARS = 300
MAX_PRIVATE_CHARS = 2000
PRIVATE_LIMIT = 100
PAGE_SIZE = 20  # entries shown at a time in Earlier
TASK_OPTIONS_LIMIT = 40
SNIPPET_CHARS = 90
DUPLICATE_WINDOW = timedelta(minutes=2)  # a double click publishes one entry
MAX_BODY_BYTES = (photos.MAX_PHOTOS + 2) * photos.MAX_BYTES  # a request bigger than this is refused
DB_ERROR = "Couldn't reach the database right now. Nothing was saved. Try again."
TABS = ("today", "earlier", "private")
TAB_LABELS = {"today": "Today’s entry", "earlier": "Earlier", "private": "Private notes"}
_TASK_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,19}")
_GOOGLE_LINK = re.compile(r"https://(?:docs|drive)\.google\.com/[^\s<>)\]]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_STATUS_RANK = {"in_progress": 0, "needs_review": 1, "todo": 2, "done": 3}
FLASH_KEY = "notebook_flash"


class LogError(Exception):
    """Something the member can fix or must be told. The message is safe to show."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _limit(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name) or default))
    except ValueError:
        return default


def tidy_limit() -> int:
    return _limit("TIDY_DAILY_LIMIT", DEFAULT_TIDY_LIMIT)


def specs_limit() -> int:
    return _limit("SPECS_DAILY_LIMIT", DEFAULT_SPECS_LIMIT)


def clean(value: str, limit: int = MAX_FIELD_CHARS) -> str:
    """What a member typed, tidied for saving: control characters out, ends trimmed."""
    return _CONTROL.sub("", (value or "").replace("\r\n", "\n")).strip()[:limit]


def valid_code(value: str | None) -> str:
    """A task code from a query string or form, or "" if it isn't shaped like one."""
    value = (value or "").strip()
    return value if _TASK_CODE.fullmatch(value) else ""


def _is_mine(note_member: str, user: User) -> bool:
    return note_member.casefold() == user["email"].casefold()


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
    something) that they haven't written up today; None if there isn't one. A draft doesn't
    count as written up: only a published entry does."""
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
                WorkNote.state == "published",
                WorkNote.posted_at >= start,
            )
        ).all()
    )
    for task_id in sorted(latest, key=latest.__getitem__, reverse=True):
        if task_id not in logged:
            return session.get(Task, task_id)
    return None


def banner(session: Session, request: Request, user: User) -> dict[str, str] | None:
    """The Home banner: the task the member worked on today and hasn't written up. None
    once they said "Not today" (until tomorrow) or when there is nothing to write."""
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


# --- how an entry stands -------------------------------------------------------------


def write_state(row: DriveWrite | None, subsystem: str) -> dict[str, Any]:
    """Where an entry stands in the Crush document, for a page to show."""
    status = row.status if row else "pending"
    return {
        "status": "written" if status == "written" else "failed" if status == "failed" else "pending",
        "text": drive_write.status_text(row, subsystem),
        "link": drive_write.doc_link(row.tab_id) if row and status == "written" else None,
    }


def state_of(note: WorkNote, write: DriveWrite | None) -> str:
    """draft, published or failed (published, but not in the Crush document)."""
    if note.state == "draft":
        return "draft"
    return "published" if write is not None and write.status == "written" else "failed"


def writes_for(session: Session, kind: str, ids: list[int]) -> dict[int, DriveWrite]:
    if not ids:
        return {}
    rows = session.exec(
        select(DriveWrite).where(DriveWrite.kind == kind, col(DriveWrite.ref_id).in_(ids))
    ).all()
    return {row.ref_id: row for row in rows}


def photos_for(session: Session, ids: list[int]) -> dict[int, list[NotePhoto]]:
    """Each entry's photos, without their bytes."""
    found: dict[int, list[NotePhoto]] = {}
    if not ids:
        return found
    rows = session.exec(
        select(NotePhoto)
        .options(defer(NotePhoto.data))
        .where(col(NotePhoto.note_id).in_(ids))
        .order_by(NotePhoto.position, NotePhoto.id)
    ).all()
    for row in rows:
        found.setdefault(row.note_id, []).append(row)
    return found


def specs_for(
    session: Session, ids: list[int], accepted_only: bool = False
) -> dict[int, list[NoteSpec]]:
    found: dict[int, list[NoteSpec]] = {}
    if not ids:
        return found
    query = select(NoteSpec).where(col(NoteSpec.note_id).in_(ids))
    if accepted_only:
        query = query.where(col(NoteSpec.accepted).is_(True))
    for row in session.exec(query.order_by(NoteSpec.position, NoteSpec.id)).all():
        found.setdefault(row.note_id, []).append(row)
    return found


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= SNIPPET_CHARS else flat[:SNIPPET_CHARS].rstrip() + "…"


def refers_view(session: Session, note: WorkNote | None) -> dict[str, Any] | None:
    """The entry another one adds to: its date and the start of what it said."""
    if note is None:
        return None
    return {
        "id": note.id,
        "date": localtime.heading(localtime.local_date(note.posted_at)),
        "snippet": _snippet(note.did),
        "task_code": note.task_code,
    }


def entry_views(
    session: Session, notes: list[WorkNote], user: User | None = None
) -> list[dict[str, Any]]:
    """Entries as the pages show them, with their writes, photos, specs and what they refer
    to loaded in a few queries rather than one per entry."""
    ids = [n.id for n in notes if n.id is not None]
    writes = writes_for(session, "note", ids)
    all_photos = photos_for(session, ids)
    drafts = [n.id for n in notes if n.state == "draft"]
    published = [n.id for n in notes if n.state != "draft"]
    kept = {**specs_for(session, drafts), **specs_for(session, published, accepted_only=True)}
    targets = {n.refers_to_id for n in notes if n.refers_to_id}
    earlier = (
        {
            n.id: n
            for n in session.exec(select(WorkNote).where(col(WorkNote.id).in_(targets))).all()
        }
        if targets
        else {}
    )
    views = []
    for note in notes:
        write = writes.get(note.id)
        state = state_of(note, write)
        note_photos = all_photos.get(note.id, [])
        views.append(
            {
                "id": note.id,
                "state": state,
                "state_label": NOTE_STATE_LABELS[state],
                "task_code": note.task_code,
                "subsystem": note.subsystem,
                "did": note.did,
                "why": note.why,
                "who": note.member_name,
                "when": note.posted_at,  # a draft's is when it was started
                "date": localtime.local_date(note.posted_at),
                "write": write_state(write, note.subsystem),
                "photos": [
                    {"id": p.id, "number": i, "in_drive": bool(p.drive_link)}
                    for i, p in enumerate(note_photos, start=1)
                ],
                "photos_stayed": sum(1 for p in note_photos if not p.drive_link)
                if note.state != "draft"
                else 0,
                "specs": [
                    {
                        "id": s.id,
                        "text": s.text,
                        "unit": s.unit,
                        "source_title": s.source_title,
                        "source_link": s.source_link,
                        "accepted": s.accepted,
                        "edited": s.edited,
                    }
                    for s in kept.get(note.id, [])
                ],
                "specs_dropped": note.specs_dropped,
                "refers": refers_view(session, earlier.get(note.refers_to_id)),
            }
        )
    return views


def note_entries(
    session: Session, subsystems: tuple[str, ...], query: str = ""
) -> list[dict[str, Any]]:
    """Published entries for some subsystems as Record entries (newest first). `query` keeps
    the ones that contain every word. A draft is never one of them."""
    notes = session.exec(
        select(WorkNote)
        .where(col(WorkNote.subsystem).in_(subsystems), WorkNote.state == "published")
        .order_by(col(WorkNote.posted_at).desc(), col(WorkNote.id).desc())
    ).all()
    words = query.casefold().split()
    matching = [
        n
        for n in notes
        if all(
            word in f"{n.member_name} {n.task_code} {n.did} {n.why}".casefold() for word in words
        )
    ]
    return [{"kind": "note", **view} for view in entry_views(session, matching)]


def has_notes(session: Session, subsystems: tuple[str, ...]) -> bool:
    return (
        session.exec(
            select(func.count())
            .select_from(WorkNote)
            .where(col(WorkNote.subsystem).in_(subsystems), WorkNote.state == "published")
        ).one()
        > 0
    )


def earlier_entries(
    session: Session, user: User, task_code: str = "", page: int = 1
) -> tuple[list[tuple[date, list[dict[str, Any]]]], bool]:
    """(the member's entries grouped by day, whether there are more), newest first,
    `PAGE_SIZE` for each page asked for so far. Their own drafts are included; nobody
    else's ever are."""
    query = select(WorkNote).where(func.lower(WorkNote.member) == user["email"].casefold())
    if task_code:
        query = query.where(WorkNote.task_code == task_code)
    shown = PAGE_SIZE * max(page, 1)
    notes = list(
        session.exec(
            query.order_by(col(WorkNote.posted_at).desc(), col(WorkNote.id).desc()).limit(
                shown + 1
            )
        ).all()
    )
    more = len(notes) > shown
    days: list[tuple[date, list[dict[str, Any]]]] = []
    for view in entry_views(session, notes[:shown], user):
        if not days or days[-1][0] != view["date"]:
            days.append((view["date"], []))
        days[-1][1].append(view)
    return days, more


def task_filter_options(session: Session, user: User) -> list[str]:
    """Task codes the member has entries on, for the filter in Earlier."""
    codes = session.exec(
        select(WorkNote.task_code)
        .where(func.lower(WorkNote.member) == user["email"].casefold())
        .distinct()
        .order_by(WorkNote.task_code)
    ).all()
    return list(codes)


# --- the member's drafts ----------------------------------------------------------------


def own_note(session: Session, user: User, note_id: int | None) -> WorkNote | None:
    note = session.get(WorkNote, note_id) if note_id else None
    return note if note is not None and _is_mine(note.member, user) else None


def own_draft(session: Session, user: User, note_id: int | None) -> WorkNote | None:
    note = own_note(session, user, note_id)
    return note if note is not None and note.state == "draft" else None


def todays_draft(session: Session, user: User, code: str = "") -> WorkNote | None:
    """The draft the member last saved today (on the task asked for, if one is), so
    reopening Today's entry shows what they were writing."""
    drafts = session.exec(
        select(WorkNote)
        .where(
            func.lower(WorkNote.member) == user["email"].casefold(),
            WorkNote.state == "draft",
            WorkNote.updated_at >= localtime.day_start_utc(),
        )
        .order_by(col(WorkNote.updated_at).desc(), col(WorkNote.id).desc())
    ).all()
    if code:
        return next((d for d in drafts if d.task_code == code), None)
    return drafts[0] if drafts else None


def own_published(session: Session, user: User, note_id: int | None) -> WorkNote | None:
    note = own_note(session, user, note_id)
    return note if note is not None and note.state == "published" else None


# --- tidying (one model call, nothing saved) --------------------------------------------


def tidies_today(session: Session, email: str) -> int:
    """How many tidies the member has had today: model calls made for Tidy my note that
    worked. (Calls logged before `llm_call.purpose` existed have none; they are told apart
    by the tidy model.)"""
    return session.exec(
        select(func.count())
        .select_from(LlmCall)
        .where(
            func.lower(LlmCall.member) == email.casefold(),
            (LlmCall.purpose == "tidy")
            | (col(LlmCall.purpose).is_(None) & (LlmCall.model == llm.tidy_model())),
            LlmCall.status == "ok",
            LlmCall.created_at >= localtime.day_start_utc(),
        )
    ).one()


def specs_today(session: Session, email: str) -> int:
    """How many times the member pressed Suggest specs today. A run is one or more model
    calls (it may look something up first); each run has exactly one call that isn't a
    tool request (its answer, or its failure), and that is the one counted."""
    return session.exec(
        select(func.count())
        .select_from(LlmCall)
        .where(
            func.lower(LlmCall.member) == email.casefold(),
            LlmCall.purpose == "specs",
            col(LlmCall.tool_called).is_(False),
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
    found.extend(
        {"title": "", "link": link}
        for link in dict.fromkeys(_GOOGLE_LINK.findall(text))
        if link not in known
    )
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
            f"You've used today's {tidy_limit()} tidies. You can still publish your note as written."
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
        coach._add_llm_calls(session, task.id, user["email"], calls, "tidy")
        session.commit()
    except Exception as exc:  # noqa: BLE001  (the member still gets their tidied note)
        session.rollback()
        log.error("Could not log a tidy (%s)", type(exc).__name__)
    if result is None:
        raise LogError(f"{error.message} You can still publish your note as written.", 502)
    return result


# --- suggesting specs (one model run, on the button) --------------------------------------


def suggest_specs(session: Session, user: User, note: WorkNote) -> specs.SpecsResult:
    """Run Suggest specs for a saved draft and store what comes back as `note_spec` rows
    (none ticked: the member chooses). The run's calls are logged whether or not it worked.
    Replaces the entry's earlier suggestions. Raises LogError."""
    if not note.did.strip():
        raise LogError("Write what you did first, then ask for specs.")
    if specs_today(session, user["email"]) >= specs_limit():
        raise LogError(f"You've used today's {specs_limit()} spec suggestions.")
    task = session.get(Task, note.task_id) if note.task_id else None
    if task is None:
        raise LogError("Choose which task this is about.")
    calls: list[llm.CallRecord] = []
    result: specs.SpecsResult | None = None
    error = "The coach couldn't list specs this time. Try again."
    try:
        result = specs.suggest(
            session,
            task,
            coach.task_context(task),
            note.did,
            note.why,
            today=coach.today(),
            session_factory=lambda: Session(db.engine),
            record=calls.append,
        )
    except retrieval.IndexEmpty:
        error = coach.NO_INDEX
    except llm.LLMError as exc:
        error = exc.message
    except Exception as exc:  # noqa: BLE001  (never let the model break the panel)
        log.error("Suggesting specs failed unexpectedly (%s)", type(exc).__name__)
    try:  # the cost of every run, failed or not
        coach._add_llm_calls(session, task.id, user["email"], calls, "specs")
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not log a spec suggestion (%s)", type(exc).__name__)
    if result is None:
        raise LogError(error, 502)
    session.exec(delete(NoteSpec).where(NoteSpec.note_id == note.id))
    for position, item in enumerate(result.items):
        session.add(
            NoteSpec(
                note_id=note.id,
                position=position,
                text=item.text,
                original_text=item.text,
                unit=item.unit,
                source_title=item.source_title,
                source_link=item.source_link,
            )
        )
    note.specs_dropped = result.dropped
    note.updated_at = utc_now()
    session.commit()
    return result


def spec_counts(session: Session) -> dict[str, int]:
    """How the suggested specs fared on published entries (the measurement for the write-up):
    each suggestion with a source is exactly one of accepted as it was, accepted after the
    member changed the wording (edited), or rejected (left unticked). `dropped` are the
    suggestions thrown away for having no source, and `entries` the published entries that
    had any suggestion or drop."""
    rows = session.exec(
        select(NoteSpec.accepted, NoteSpec.edited)
        .join(WorkNote, col(WorkNote.id) == col(NoteSpec.note_id))
        .where(WorkNote.state == "published")
    ).all()
    entries = session.exec(
        select(WorkNote.id).where(
            WorkNote.state == "published",
            (WorkNote.specs_dropped > 0)
            | col(WorkNote.id).in_(select(NoteSpec.note_id)),
        )
    ).all()
    dropped = session.exec(
        select(func.coalesce(func.sum(WorkNote.specs_dropped), 0)).where(
            WorkNote.state == "published"
        )
    ).one()
    return {
        "entries": len(entries),
        "suggested": len(rows),
        "accepted": sum(1 for accepted, edited in rows if accepted and not edited),
        "edited": sum(1 for accepted, edited in rows if accepted and edited),
        "rejected": sum(1 for accepted, _ in rows if not accepted),
        "dropped": int(dropped),
    }


def apply_spec_choices(
    session: Session, note: WorkNote, keep: set[int], texts: dict[int, str]
) -> None:
    """Record what the member did with each suggested spec: ticked (accepted), changed
    (edited) or left. Only a draft's specs change."""
    rows = session.exec(select(NoteSpec).where(NoteSpec.note_id == note.id)).all()
    for row in rows:
        row.accepted = row.id in keep
        wanted = clean(texts.get(row.id, row.text), specs.MAX_SPEC_CHARS) or row.original_text
        row.text = wanted
        row.edited = wanted != row.original_text
        session.add(row)


# --- what a form posts ---------------------------------------------------------------------


@dataclass
class Submitted:
    """The entry form as posted: its text, which button was pressed, and any photos (already
    read, each at most `photos.MAX_BYTES`)."""

    do: str = "save"  # which button: save, photos, tidy, specs, publish, discard, remove-photo-N
    task: str = ""
    did: str = ""
    why: str = ""
    entry: int | None = None
    refers: int | None = None
    original_did: str = ""
    original_why: str = ""
    asked: str = ""
    reply: str = ""
    keep: set[int] = field(default_factory=set)
    spec_text: dict[int, str] = field(default_factory=dict)
    files: list[tuple[str, bytes]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # photos not read (too big, too many)


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, str) and value.strip().isdigit() else None


async def read_submitted(request: Request, action: str) -> Submitted:
    """Read an entry form (multipart, because it may carry photos). Refuses a body larger
    than six photos could be, before parsing it."""
    length = _int(request.headers.get("content-length"))
    if length is not None and length > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Too much data")
    form = await request.form()
    sub = Submitted(
        do=action,
        task=valid_code(str(form.get("task") or "")),
        did=str(form.get("did") or ""),
        why=str(form.get("why") or ""),
        entry=_int(form.get("entry")),
        refers=_int(form.get("refers")),
        original_did=str(form.get("original_did") or ""),
        original_why=str(form.get("original_why") or ""),
        asked=str(form.get("asked") or ""),
        reply=str(form.get("reply") or ""),
    )
    sub.keep = {n for n in (_int(v) for v in form.getlist("keep")) if n is not None}
    for key in form:
        if key.startswith("spec_text_") and (n := _int(key.removeprefix("spec_text_"))):
            sub.spec_text[n] = str(form.get(key) or "")
    for upload in form.getlist("photos"):
        if isinstance(upload, str) or not getattr(upload, "filename", ""):
            continue  # an empty file box
        if len(sub.files) + len(sub.refused) >= photos.MAX_PHOTOS + 2:
            sub.refused.append(upload.filename)
            continue
        data = await upload.read(photos.MAX_BYTES + 1)
        if len(data) > photos.MAX_BYTES:
            sub.refused.append(upload.filename)
        else:
            sub.files.append((upload.filename, data))
    return sub


# --- saving a draft ----------------------------------------------------------------------


def _task(session: Session, code: str) -> Task:
    task = session.exec(select(Task).where(Task.code == code)).first() if code else None
    if task is None:
        raise LogError("Choose which task this is about.")
    return task


def save_draft(session: Session, user: User, sub: Submitted, *, create: bool = True) -> WorkNote | None:
    """Store the form's text on the member's draft (made if there isn't one yet and
    `create`). Raises LogError for someone else's entry or a published one."""
    note = None
    if sub.entry is not None:
        note = own_draft(session, user, sub.entry)
        if note is None:
            raise LogError("That draft isn't there any more. Start a new entry.", 404)
    elif not create:
        return None
    task = _task(session, sub.task or (note.task_code if note else ""))
    did, why = clean(sub.did), clean(sub.why)
    original_did, original_why = clean(sub.original_did), clean(sub.original_why)
    if original_did:  # the member's own words from before they used Tidy
        original = original_did + (f"\n\nWhy: {original_why}" if original_why else "")
    else:
        original = did + (f"\n\nWhy: {why}" if why else "")
    now = utc_now()
    if note is None:
        refers = own_published(session, user, sub.refers)
        note = WorkNote(
            member=user["email"],
            member_name=user["name"],
            task_id=task.id,
            task_code=task.code,
            subsystem=task.subsystem,
            did=did,
            why=why,
            original=original,
            state="draft",
            refers_to_id=refers.id if refers else None,
            posted_at=now,
            updated_at=now,
        )
        session.add(note)
    else:
        note.task_id, note.task_code, note.subsystem = task.id, task.code, task.subsystem
        note.did, note.why, note.original = did, why, original
        note.updated_at = now
    session.commit()
    session.refresh(note)
    return note


def attach_photos(
    session: Session, user: User, note: WorkNote, sub: Submitted
) -> tuple[int, list[str]]:
    """Add the posted photos to a draft: (how many were kept, what to tell the member about
    the rest). Each is opened as a picture and re-saved without its location data
    (`photos.clean`); one bad photo doesn't stop the others."""
    problems = [
        f"“{name}” is over {photos.MAX_BYTES / 1_000_000:.1f} MB. Photos are shrunk in the browser; "
        "try a smaller one."
        for name in sub.refused
    ]
    existing = session.exec(
        select(func.count()).select_from(NotePhoto).where(NotePhoto.note_id == note.id)
    ).one()
    next_position = (
        session.exec(
            select(func.max(NotePhoto.position)).where(NotePhoto.note_id == note.id)
        ).one()
        or 0
    ) + 1 if existing else 0
    added = 0
    for name, raw in sub.files:
        if existing + added >= photos.MAX_PHOTOS:
            problems.append(
                f"An entry holds up to {photos.MAX_PHOTOS} photos, so “{name}” wasn't added."
            )
            continue
        try:
            kept = photos.clean(raw, name)
        except photos.PhotoError as exc:
            problems.append(str(exc))
            continue
        session.add(
            NotePhoto(
                note_id=note.id,
                member=user["email"],
                position=next_position + added,
                size=len(kept.data),
                width=kept.width,
                height=kept.height,
                data=kept.data,
            )
        )
        added += 1
    if added:
        note.updated_at = utc_now()
        session.commit()
    return added, problems


# --- publishing ----------------------------------------------------------------------------


def publish(session: Session, user: User, sub: Submitted) -> tuple[WorkNote, drive_write.Published]:
    """Publish the entry: it becomes read-only, then is written to the Crush document with
    its accepted specs and photo links. Publishing the same entry twice, or the same words
    twice in a row, publishes once. Raises LogError."""
    note = None
    if sub.entry is not None:
        note = own_note(session, user, sub.entry)
        if note is None:
            raise LogError("That draft isn't there any more. Start a new entry.", 404)
    if note is not None and note.state == "published":  # a double click, or a stale page
        return note, drive_write.publish_note(session, note)
    if not clean(sub.did):
        raise LogError("Write what you did before publishing.")
    if note is None:
        task = _task(session, sub.task)
        duplicate = session.exec(
            select(WorkNote).where(
                func.lower(WorkNote.member) == user["email"].casefold(),
                WorkNote.task_id == task.id,
                WorkNote.state == "published",
                WorkNote.did == clean(sub.did),
                WorkNote.why == clean(sub.why),
                WorkNote.posted_at >= utc_now() - DUPLICATE_WINDOW,
            )
        ).first()
        if duplicate is not None:
            return duplicate, drive_write.publish_note(session, duplicate)
    note = save_draft(session, user, sub)
    apply_spec_choices(session, note, sub.keep, sub.spec_text)
    now = utc_now()
    note.state, note.posted_at, note.updated_at = "published", now, now
    session.commit()
    session.refresh(note)
    return note, drive_write.publish_note(session, note)  # after the commit; never raises


def publish_notice(note: WorkNote, result: drive_write.Published) -> tuple[str, bool]:
    """(what to tell the member after publishing, whether it is a problem)."""
    write = write_state(result.write, note.subsystem)
    stayed = result.photos.stayed
    if write["status"] != "written":
        text = (
            f"Published here, but it didn’t reach the Crush document. {write['text']} "
            "Use Retry under Earlier."
        )
        return text, True
    text = f"Published. {write['text']}"
    if stayed:
        many = "photos" if stayed != 1 else "photo"
        reason = f" {result.photos.error}" if result.photos.error else ""
        text += f" The {stayed} {many} stayed in the app, not in Drive.{reason}"
    return text, bool(stayed)


# --- the panel ------------------------------------------------------------------------------


def todays_form(session: Session, user: User, code: str, entry_id: int | None, refers_id: int | None,
                form: dict[str, Any] | None) -> dict[str, Any]:
    """What Today's entry shows: the task picker, the draft being written (or a blank
    entry), and the entry a new one follows up."""
    draft = own_draft(session, user, entry_id) if entry_id else None
    if draft is None and not entry_id and not refers_id:
        draft = todays_draft(session, user, code)
    refers = own_published(session, user, refers_id or (draft.refers_to_id if draft else None))
    if refers is not None and not code and draft is None:
        code = refers.task_code
    tasks, task = task_choices(session, user, (form or {}).get("task") or (draft.task_code if draft else code))
    entry = entry_views(session, [draft], user)[0] if draft else None
    values = dict(form or {})
    if draft is not None:
        values.setdefault("did", draft.did)
        values.setdefault("why", draft.why)
    return {
        "tasks": tasks,
        "task": task,
        "entry": entry,
        "form": values,
        "refers": refers_view(session, refers),
        "photos_left": photos.MAX_PHOTOS - len(entry["photos"]) if entry else photos.MAX_PHOTOS,
    }


def panel_context(
    session: Session,
    user: User,
    *,
    tab: str = "today",
    show: tuple[str, ...] | None = None,
    code: str = "",
    entry_id: int | None = None,
    refers_id: int | None = None,
    form: dict[str, Any] | None = None,
    error: str | None = None,
    notice: str | None = None,
    posted: dict[str, Any] | None = None,
    status: int = 200,
    task_filter: str = "",
    page: int = 1,
) -> dict[str, Any]:
    """Everything the notebook templates show. `show` lists the sections to load (the panel
    shows one; the page shows Today's entry and one of the others). A failure loading one
    section costs only that section."""
    tab = tab if tab in TABS else "today"
    show = show or (tab,)
    context: dict[str, Any] = {
        "user": user,
        "tab": tab,
        "tabs": [(slug, TAB_LABELS[slug]) for slug in TABS],
        "show": show,
        "error": error,
        "notice": notice,
        "posted": posted,
        "form": form or {},
        "tasks": [],
        "task": None,
        "entry": None,
        "refers": None,
        "photos_left": photos.MAX_PHOTOS,
        "days": [],
        "more": False,
        "page": page,
        "filter": task_filter,
        "filter_options": [],
        "private_notes": [],
        "load_errors": {},
        "tidy_limit": tidy_limit(),
        "specs_limit": specs_limit(),
        "max_photos": photos.MAX_PHOTOS,
        "max_photo_bytes": photos.MAX_BYTES,
        "doc_link": drive_write.doc_link(),
        "status": status,
    }
    for section in show:
        try:
            if section == "today":
                context.update(todays_form(session, user, code, entry_id, refers_id, form))
            elif section == "earlier":
                context["days"], context["more"] = earlier_entries(
                    session, user, valid_code(task_filter), page
                )
                context["filter_options"] = task_filter_options(session, user)
            else:
                context["private_notes"] = private_notes(session, user)
        except Exception as exc:  # noqa: BLE001  (a database failure must not break the page)
            session.rollback()
            log.error("Could not load the notebook (%s)", type(exc).__name__)
            context["load_errors"][section] = (
                "Couldn't load this from the database right now. Try again in a moment."
            )
    return context


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render_panel(request: Request, session: Session, user: User, **kwargs: Any) -> Response:
    """The panel as a fragment (htmx and `notebook.js`), or as a whole page otherwise."""
    if is_htmx(request):
        context = panel_context(session, user, **kwargs)
        return templates.TemplateResponse(
            request, "_notebook_panel.html", {**context, "panel": True},
            status_code=context["status"],
        )
    return render_page(request, session, user, **kwargs)


def render_page(request: Request, session: Session, user: User, **kwargs: Any) -> Response:
    """The whole page: Today's entry beside Earlier (or Private notes)."""
    side = "private" if kwargs.get("tab") == "private" else "earlier"
    kwargs["tab"] = side if kwargs.get("tab") in ("earlier", "private") else "today"
    context = panel_context(session, user, show=("today", side), **kwargs)
    context["side"] = side
    return templates.TemplateResponse(
        request, "notebook.html", {**context, "panel": False}, status_code=context["status"]
    )


def flash(request: Request, *, notice: str | None = None, error: str | None = None,
          posted: int | None = None) -> None:
    """A message kept for the next page (after a redirect, for forms posted without htmx)."""
    request.session[FLASH_KEY] = {"notice": notice, "error": error, "posted": posted}


def take_flash(request: Request) -> dict[str, Any]:
    found = request.session.pop(FLASH_KEY, None)
    return found if isinstance(found, dict) else {}


def posted_for(session: Session, user: User, note_id: int | None) -> dict[str, Any] | None:
    """How a just-published or retried entry stands, for the notice under Earlier."""
    note = own_published(session, user, note_id)
    if note is None:
        return None
    row = session.exec(
        select(DriveWrite).where(DriveWrite.kind == "note", DriveWrite.ref_id == note.id)
    ).first()
    return {"id": note.id, "write": write_state(row, note.subsystem)}


# --- private notes ----------------------------------------------------------------------------


def private_notes(session: Session, user: User) -> list[dict[str, Any]]:
    """The member's own private notes, newest first. Every read of this table filters on the
    signed-in member; there is no other query for it."""
    rows = session.exec(
        select(PrivateNote)
        .where(func.lower(PrivateNote.member) == user["email"].casefold())
        .order_by(col(PrivateNote.created_at).desc(), col(PrivateNote.id).desc())
        .limit(PRIVATE_LIMIT)
    ).all()
    return [
        {"id": r.id, "text": r.text, "when": r.created_at,
         "date": localtime.local_date(r.created_at)}
        for r in rows
    ]


# --- routes: pages and fragments --------------------------------------------------------------


def _query(tab: str, task: str, entry: str, refers: str, flt: str, page: str) -> dict[str, Any]:
    return {
        "tab": tab if tab in TABS else "today",
        "code": valid_code(task),
        "entry_id": _int(entry),
        "refers_id": _int(refers),
        "task_filter": valid_code(flt),
        "page": max(1, min(_int(page) or 1, 50)),
    }


@router.get("/notebook", response_class=HTMLResponse)
def notebook_page(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: MaybeUser,
    tab: str = "today",
    task: str = "",
    entry: str = "",
    refers: str = "",
    filter: str = "",
    page: str = "1",
    posted: str = "",
):
    if user is None:  # signed out: nothing is read
        return locked_page(request, "the Notebook")
    message = take_flash(request)
    wanted = _int(posted) or message.get("posted")
    return render_page(
        request,
        session,
        user,
        **_query(tab, task, entry, refers, filter, page),
        error=message.get("error"),
        notice=message.get("notice"),
        posted=posted_for(session, user, wanted),
    )


@router.get("/notebook/panel", response_class=HTMLResponse)
def notebook_panel(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    tab: str = "today",
    task: str = "",
    entry: str = "",
    refers: str = "",
    filter: str = "",
    page: str = "1",
):
    context = panel_context(session, user, **_query(tab, task, entry, refers, filter, page))
    return templates.TemplateResponse(
        request, "_notebook_panel.html", {**context, "panel": True}, status_code=context["status"]
    )


@router.get("/worklog", response_class=RedirectResponse)
def old_worklog_page(task: str = "", tab: str = "log"):
    """The work log's old address: the Notebook, on the matching tab."""
    query = {"tab": "earlier" if tab == "notebook" else "today", "task": valid_code(task)}
    return RedirectResponse(f"/notebook?{urlencode({k: v for k, v in query.items() if v})}", status_code=303)


@router.get("/worklog/panel", response_class=RedirectResponse)
def old_worklog_panel(task: str = "", tab: str = "log"):
    query = {"tab": "earlier" if tab == "notebook" else "today", "task": valid_code(task)}
    return RedirectResponse(
        f"/notebook/panel?{urlencode({k: v for k, v in query.items() if v})}", status_code=303
    )


# --- routes: the entry form ---------------------------------------------------------------------


def _respond(
    request: Request,
    session: Session,
    user: User,
    *,
    tab: str = "today",
    entry_id: int | None = None,
    notice: str | None = None,
    error: str | None = None,
    posted: int | None = None,
    form: dict[str, Any] | None = None,
    code: str = "",
    status: int = 200,
) -> Response:
    """Show the result of a form. With htmx it is the panel itself; without, an error shows
    the page where it stands and a success redirects (so a reload doesn't post again),
    keeping the message for the next page."""
    if is_htmx(request) or status >= 400:
        return render_panel(
            request, session, user, tab=tab, entry_id=entry_id, notice=notice, error=error,
            posted=posted_for(session, user, posted), form=form, code=code, status=status,
        )
    flash(request, notice=notice, error=error, posted=posted)
    query = {"tab": tab, "entry": entry_id or ""}
    return RedirectResponse(
        "/notebook?" + urlencode({k: v for k, v in query.items() if v}), status_code=303
    )


ACTIONS = re.compile(r"save|photos|tidy|specs|publish|discard|remove-photo-\d+")


@router.post("/notebook/entry/{action}", response_class=HTMLResponse)
async def entry_form(
    request: Request,
    action: str,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
):
    """Every button of the entry form: save (and add photos), tidy, suggest specs, publish,
    discard, or remove a photo (the `action`). The form is multipart because it may carry
    photos."""
    if not ACTIONS.fullmatch(action):
        raise HTTPException(status_code=404)
    sub = await read_submitted(request, action)
    return await run_in_threadpool(handle_entry, request, session, user, sub)


def handle_entry(request: Request, session: Session, user: User, sub: Submitted) -> Response:
    form = {
        "task": sub.task,
        "did": clean(sub.did),
        "why": clean(sub.why),
        "original_did": clean(sub.original_did),
        "original_why": clean(sub.original_why),
        "tidied": bool(clean(sub.original_did)),
    }
    entry_id = sub.entry
    try:
        return _handle(request, session, user, sub, form)
    except LogError as exc:
        session.rollback()
        return _respond(request, session, user, entry_id=entry_id, error=exc.message,
                        form=form, status=exc.status, code=sub.task)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not handle a notebook entry (%s)", type(exc).__name__)
        return _respond(request, session, user, entry_id=entry_id, error=DB_ERROR,
                        form=form, status=503, code=sub.task)


def _handle(request: Request, session: Session, user: User, sub: Submitted, form: dict[str, Any]) -> Response:
    action = sub.do
    if action == "publish":
        note, result = publish(session, user, sub)
        text, problem = publish_notice(note, result)
        return _respond(request, session, user, tab="earlier", notice=None if problem else text,
                        error=text if problem else None, posted=note.id)
    if action == "discard":
        note = own_draft(session, user, sub.entry)
        if note is None:
            raise LogError("That draft isn't there any more.", 404)
        discard(session, note)
        return _respond(request, session, user, notice="Draft discarded.")
    # Everything else works on a draft, made now if the member has typed or added something.
    has_input = bool(clean(sub.did) or clean(sub.why) or sub.files or sub.refused or sub.entry)
    if not has_input:
        raise LogError("Write something or add a photo first.")
    if action == "tidy":
        return tidy_form(request, session, user, sub, form)
    note = save_draft(session, user, sub)
    added, problems = attach_photos(session, user, note, sub)
    apply_spec_choices(session, note, sub.keep, sub.spec_text)
    session.commit()
    if action.startswith("remove-photo-"):
        photo_id = _int(action.removeprefix("remove-photo-"))
        removed = session.exec(
            select(NotePhoto).options(defer(NotePhoto.data)).where(
                NotePhoto.id == photo_id, NotePhoto.note_id == note.id
            )
        ).first()
        if removed is not None:
            session.delete(removed)
            session.commit()
        return _respond(request, session, user, entry_id=note.id, notice="Photo removed.")
    if action == "specs":
        try:
            result = suggest_specs(session, user, note)
        except LogError as exc:
            session.rollback()
            return _respond(request, session, user, entry_id=note.id, error=exc.message,
                            status=exc.status)
        count = len(result.items)
        notice = f"{count} spec{'s' if count != 1 else ''} suggested. Tick the ones to keep."
        if not count:
            notice = "No suggestion had a source, so there is nothing to add."
        return _respond(request, session, user, entry_id=note.id, notice=notice)
    # save, or photos added
    if problems:
        return _respond(request, session, user, entry_id=note.id,
                        notice=f"{added} photo{'s' if added != 1 else ''} added." if added else None,
                        error=" ".join(problems))
    saved = f"{added} photo{'s' if added != 1 else ''} added." if added and action == "photos" else "Draft saved."
    return _respond(request, session, user, entry_id=note.id, notice=saved)


def tidy_form(request: Request, session: Session, user: User, sub: Submitted, form: dict[str, Any]) -> Response:
    """Tidy my note: the one model call. Shows the result for the member to edit; the text is
    saved nowhere, but photos chosen with it are kept on the draft they belong to."""
    note = None
    if sub.files or sub.refused or sub.entry:
        note = save_draft(session, user, sub, create=bool(sub.files or sub.refused))
        if note is not None:
            attach_photos(session, user, note, sub)
            apply_spec_choices(session, note, sub.keep, sub.spec_text)
            session.commit()
    form["original_did"] = clean(sub.original_did) or clean(sub.did)
    form["original_why"] = clean(sub.original_why) if sub.original_did else clean(sub.why)
    result = tidy(session, user, sub.task, sub.did, sub.why, sub.asked, sub.reply)
    form.update(did=result.did, why=result.why, tidied=True, question=result.question,
                kept_original=result.kept_original)
    return render_panel(request, session, user, entry_id=note.id if note else sub.entry,
                        form=form, code=sub.task, refers_id=sub.refers)


def discard(session: Session, note: WorkNote) -> None:
    """Throw a draft away, with its photos and suggested specs."""
    session.exec(delete(NotePhoto).where(NotePhoto.note_id == note.id))
    session.exec(delete(NoteSpec).where(NoteSpec.note_id == note.id))
    session.delete(note)
    session.commit()


@router.post("/notebook/entries/{note_id}/retry", response_class=HTMLResponse)
def retry_entry(
    request: Request,
    note_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
):
    """Retry a published entry the Crush document wouldn't take: the author's, or a lead's."""
    try:
        note = session.get(WorkNote, note_id)
        if note is None or note.state != "published" or (
            not _is_mine(note.member, user) and user["role"] != "lead"
        ):
            raise LogError("That entry isn't in your notebook.", 404)
        result = drive_write.publish_note(session, note)
        text, problem = publish_notice(note, result)
    except LogError as exc:
        session.rollback()
        return _respond(request, session, user, tab="earlier", error=exc.message, status=exc.status)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not retry an entry's write (%s)", type(exc).__name__)
        return _respond(request, session, user, tab="earlier", error=DB_ERROR, status=503)
    if not is_htmx(request):
        flash(request, notice=None if problem else text, error=text if problem else None,
              posted=note.id)
        return RedirectResponse("/notebook?tab=earlier", status_code=303)
    return render_panel(
        request, session, user, tab="earlier", notice=None if problem else text,
        error=text if problem else None, posted=posted_for(session, user, note.id),
    )


# --- routes: photos ------------------------------------------------------------------------------


@router.get("/notebook/photos/{photo_id}")
def photo_file(
    photo_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
):
    """A photo: a draft's only to its member, a published entry's to any signed-in member.
    Anyone else gets the same 404 as a photo that doesn't exist."""
    row = session.get(NotePhoto, photo_id)
    note = session.get(WorkNote, row.note_id) if row is not None else None
    if row is None or note is None or (note.state == "draft" and not _is_mine(note.member, user)):
        raise HTTPException(status_code=404)
    return Response(
        row.data,
        media_type=row.content_type,
        headers={
            "Cache-Control": "private, no-store" if note.state == "draft" else "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )


# --- routes: private notes -----------------------------------------------------------------------


def _private_response(request: Request, session: Session, user: User, **kwargs: Any) -> Response:
    if is_htmx(request) or kwargs.get("error"):
        return render_panel(request, session, user, tab="private", **kwargs)
    return RedirectResponse("/notebook?tab=private", status_code=303)


@router.post("/notebook/private", response_class=HTMLResponse)
def add_private_note(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    text: Annotated[str, Form()] = "",
):
    """Save a private note: only words, the member and the time. It is not tied to a task, not
    written to Drive, not sent to a model and not shown to anyone else."""
    body = clean(text, MAX_PRIVATE_CHARS)
    if not body:
        return _private_response(request, session, user, error="Write something first.", status=400)
    try:
        session.add(PrivateNote(member=user["email"], text=body))
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not save a private note (%s)", type(exc).__name__)
        return _private_response(request, session, user, error=DB_ERROR, status=503)
    return _private_response(request, session, user, notice="Private note saved.")


@router.post("/notebook/private/{note_id}/delete", response_class=HTMLResponse)
def delete_private_note(
    request: Request,
    note_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
):
    row = session.get(PrivateNote, note_id)
    if row is None or not _is_mine(row.member, user):
        raise HTTPException(status_code=404)  # someone else's note is the same as none
    session.delete(row)
    session.commit()
    return _private_response(request, session, user, notice="Private note deleted.")


# --- routes: the Home banner ------------------------------------------------------------------------


@router.post("/notebook/dismiss", response_class=Response)
@router.post("/worklog/dismiss", response_class=Response)
def not_today(request: Request, user: CurrentUser, next: Annotated[str, Form()] = "/"):
    """"Not today" on the Home banner: hide it until tomorrow."""
    request.session["worklog_dismissed"] = localtime.today().isoformat()
    return RedirectResponse(workflow._safe_next(next, "/"), status_code=303)
