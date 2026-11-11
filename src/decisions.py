"""Decision records: /decisions, a decision's page, and who may confirm or supersede.

Members propose. Only leads (LEAD_EMAILS) accept (confirm) or supersede, and the
server checks that on every POST; the buttons are just hidden from members.
A confirmed decision outranks Drive search results (CLAUDE.md), so the coach looks
decisions up first (`search_confirmed`, called through `coach.py`).

The helpers here take plain values and a Session, not a Request, so tests and the
coach can use them directly.
"""

import logging
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, select

import db
from auth import CurrentUser, LeadUser, User
from models import (
    DECISION_STATUS_LABELS,
    DECISION_STATUSES,
    SUBSYSTEMS,
    Decision,
    DecisionTask,
    Task,
    utc_now,
)

log = logging.getLogger("crush_coach")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()

ALTERNATIVE_ROWS = 3  # blank rows on the form; the form needs no JavaScript
SOURCE_ROWS = 3
MAX_TASK_CODES = 10
LIMITS = {
    "topic": 200,
    "decision": 2000,
    "rationale": 4000,
    "option": 300,
    "rejected_because": 1000,
    "title": 200,
    "link": 500,
    "folder": 300,
    "tasks": 200,
}
MIN_YEAR, MAX_YEAR = 1990, 2100
DB_ERROR = "Couldn't reach the database right now. Nothing was saved. Try again."

# Only Google Docs/Drive links, the same rule the coach's link renderer uses.
_DRIVE_LINK = re.compile(r"^https://(?:docs|drive)\.google\.com/\S+$")
_SEARCH_TERM = re.compile(r"[a-z0-9][a-z0-9/-]+")
# Confirmed decisions come first on every page: they are the ones people build from.
_STATUS_ORDER = {"confirmed": 0, "proposed": 1, "superseded": 2}


class DecisionError(Exception):
    """A rule was broken (wrong status, not allowed). The message is safe to show."""


# --- the form ---------------------------------------------------------------


def blank_form(**values: str) -> dict[str, Any]:
    """An empty form, with blank alternative and source rows."""
    form: dict[str, Any] = {
        "subsystem": "",
        "topic": "",
        "decision": "",
        "rationale": "",
        "tasks": "",
        "drafted": False,
        "alternatives": [],
        "sources": [],
    }
    form.update(values)
    return pad_rows(form)


def pad_rows(form: dict[str, Any]) -> dict[str, Any]:
    """Add blank rows so the form always offers some, and never fewer than it has."""
    form["alternatives"] = list(form["alternatives"])
    form["sources"] = list(form["sources"])
    while len(form["alternatives"]) < ALTERNATIVE_ROWS:
        form["alternatives"].append({"option": "", "rejected_because": ""})
    while len(form["sources"]) < SOURCE_ROWS:
        form["sources"].append({"title": "", "link": "", "folder": "", "year": ""})
    return form


def read_form(data: Mapping[str, Any]) -> dict[str, Any]:
    """What the browser posted, as text, in the shape the form template shows."""

    def text(name: str) -> str:
        value = data.get(name)
        return value.strip() if isinstance(value, str) else ""

    alternatives = [
        {
            "option": text(f"alt_option_{i}"),
            "rejected_because": text(f"alt_why_{i}"),
        }
        for i in range(_row_count(data, "alt_option_"))
    ]
    sources = [
        {
            "title": text(f"src_title_{i}"),
            "link": text(f"src_link_{i}"),
            "folder": text(f"src_folder_{i}"),
            "year": text(f"src_year_{i}"),
        }
        for i in range(_row_count(data, "src_title_"))
    ]
    return pad_rows(
        {
            "subsystem": text("subsystem"),
            "topic": text("topic"),
            "decision": text("decision"),
            "rationale": text("rationale"),
            "tasks": text("tasks"),
            "drafted": text("drafted") == "1",
            "alternatives": alternatives,
            "sources": sources,
        }
    )


def _row_count(data: Mapping[str, Any], prefix: str) -> int:
    indexes = [
        int(key[len(prefix) :])
        for key in data
        if isinstance(key, str)
        and key.startswith(prefix)
        and key[len(prefix) :].isdigit()
    ]
    return min(max(indexes) + 1, 20) if indexes else 0  # 20: never trust the count


def parse_task_codes(text: str) -> list[str]:
    """Task codes typed as "M04, E02" or "M04 E02": upper-cased, no repeats."""
    codes: list[str] = []
    for code in re.split(r"[,\s]+", text.strip().upper()):
        if code and code not in codes:
            codes.append(code)
    return codes


def validate(form: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Check a form. Returns (clean values, plain-language problems)."""
    problems: list[str] = []

    def required(name: str, label: str) -> str:
        value = form[name]
        if not value:
            problems.append(f"{label} is required.")
        elif len(value) > LIMITS[name]:
            problems.append(f"{label} is too long (up to {LIMITS[name]} characters).")
        return value

    def too_long(value: str, key: str, label: str) -> None:
        if len(value) > LIMITS[key]:
            problems.append(f"{label} is too long (up to {LIMITS[key]} characters).")

    subsystem = form["subsystem"]
    if subsystem not in SUBSYSTEMS:
        problems.append("Choose a subsystem.")
    topic = required("topic", "Topic")
    decision = required("decision", "Decision")
    rationale = required("rationale", "Rationale")

    alternatives = []
    for i, row in enumerate(form["alternatives"], start=1):
        if not row["option"] and not row["rejected_because"]:
            continue
        if not row["option"] or not row["rejected_because"]:
            problems.append(
                f"Alternative {i} needs both the option and why it was rejected."
            )
            continue
        too_long(row["option"], "option", f"Alternative {i}")
        too_long(row["rejected_because"], "rejected_because", f"Why alternative {i} was rejected")
        alternatives.append(dict(row))

    sources = []
    for i, row in enumerate(form["sources"], start=1):
        if not any(row.values()):
            continue
        if not row["title"] or not row["link"]:
            problems.append(f"Source {i} needs a title and a Drive link.")
            continue
        if not _DRIVE_LINK.match(row["link"]):
            problems.append(
                f"Source {i}: the link must start with https://docs.google.com/ "
                "or https://drive.google.com/."
            )
            continue
        year: int | None = None
        if row["year"]:
            if not row["year"].isdigit() or not MIN_YEAR <= int(row["year"]) <= MAX_YEAR:
                problems.append(f"Source {i}: the year must be four digits, like 2026.")
                continue
            year = int(row["year"])
        too_long(row["title"], "title", f"Source {i} title")
        too_long(row["link"], "link", f"Source {i} link")
        too_long(row["folder"], "folder", f"Source {i} folder")
        sources.append(
            {
                "title": row["title"],
                "link": row["link"],
                "folder": row["folder"],
                "year": year,
            }
        )

    too_long(form["tasks"], "tasks", "Related tasks")
    codes = parse_task_codes(form["tasks"])
    if len(codes) > MAX_TASK_CODES:
        problems.append(f"Link at most {MAX_TASK_CODES} tasks.")

    clean = {
        "subsystem": subsystem,
        "topic": topic,
        "decision": decision,
        "rationale": rationale,
        "alternatives": alternatives,
        "sources": sources,
        "task_codes": codes,
    }
    return clean, problems


def form_from_draft(data: Mapping[str, Any], task: Task) -> dict[str, Any]:
    """A coach draft (model JSON) as a pre-filled form. Unusable parts are left blank."""

    def text(value: Any) -> str:
        # Years may come back as numbers; anything else that isn't text is dropped.
        if isinstance(value, bool) or not isinstance(value, str | int):
            return ""
        return str(value).strip()

    def rows(name: str, keys: tuple[str, ...]) -> list[dict[str, str]]:
        found = data.get(name)
        if not isinstance(found, list):
            return []
        return [
            {key: text(item.get(key)) for key in keys}
            for item in found
            if isinstance(item, dict)
        ]

    subsystem = text(data.get("subsystem"))
    return blank_form(
        subsystem=subsystem if subsystem in SUBSYSTEMS else task.subsystem,
        topic=text(data.get("topic")),
        decision=text(data.get("decision")),
        rationale=text(data.get("rationale")),
        tasks=task.code,
        drafted=True,
        alternatives=rows("alternatives", ("option", "rejected_because")),
        sources=rows("sources", ("title", "link", "folder", "year")),
    )


# --- queries ----------------------------------------------------------------


def find_tasks(session: Session, codes: list[str]) -> tuple[list[Task], list[str]]:
    """The tasks for these codes, and the codes that match no task."""
    if not codes:
        return [], []
    tasks = list(session.exec(select(Task).where(col(Task.code).in_(codes))).all())
    known = {t.code for t in tasks}
    return tasks, [c for c in codes if c not in known]


def create_decision(
    session: Session, clean: dict[str, Any], tasks: list[Task], proposer: str, *, drafted: bool
) -> Decision:
    decision = Decision(
        subsystem=clean["subsystem"],
        topic=clean["topic"],
        decision=clean["decision"],
        rationale=clean["rationale"],
        alternatives=clean["alternatives"],
        sources=clean["sources"],
        status="proposed",
        proposed_by=proposer,
        drafted_by_coach=drafted,
    )
    session.add(decision)
    session.flush()  # get the id for the task links
    for task in tasks:
        session.add(DecisionTask(decision_id=decision.id, task_id=task.id))
    session.commit()
    return decision


def decisions_for_task(session: Session, task_id: int) -> list[Decision]:
    """Decisions linked to a task: confirmed first, then proposed, then superseded."""
    found = session.exec(
        select(Decision)
        .join(DecisionTask, col(DecisionTask.decision_id) == col(Decision.id))
        .where(DecisionTask.task_id == task_id)
        .order_by(col(Decision.created_at).desc())
    ).all()
    return sorted(found, key=lambda d: _STATUS_ORDER.get(d.status, len(_STATUS_ORDER)))


def task_codes_for(session: Session, decision_id: int) -> list[str]:
    return list(
        session.exec(
            select(Task.code)
            .join(DecisionTask, col(DecisionTask.task_id) == col(Task.id))
            .where(DecisionTask.decision_id == decision_id)
            .order_by(Task.code)
        ).all()
    )


def search_confirmed(
    session: Session,
    query: str = "",
    subsystem: str = "",
    task_id: int | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Confirmed decisions matching some keywords, shaped for the coach.

    Only confirmed decisions: a proposal is not binding. Decisions linked to
    `task_id` rank first, then by how many keywords match. Matching is done in
    Python so it behaves the same on SQLite and Postgres (the table is small).
    """
    statement = select(Decision).where(Decision.status == "confirmed")
    if subsystem in SUBSYSTEMS:
        statement = statement.where(Decision.subsystem == subsystem)
    candidates = list(session.exec(statement).all())

    linked: set[int] = set()
    if task_id is not None:
        linked = set(
            session.exec(
                select(DecisionTask.decision_id).where(DecisionTask.task_id == task_id)
            ).all()
        )

    terms = list(dict.fromkeys(_SEARCH_TERM.findall(query.casefold())))[:8]
    ranked: list[tuple[int, int, datetime, Decision]] = []
    for d in candidates:
        haystack = f"{d.topic} {d.decision} {d.rationale}".casefold()
        hits = sum(1 for term in terms if term in haystack)
        if terms and not hits and d.id not in linked:
            continue
        ranked.append((int(d.id in linked), hits, d.confirmed_at or d.created_at, d))
    ranked.sort(key=lambda row: row[:3], reverse=True)

    return [
        {
            "id": d.id,
            "link": f"/decisions/{d.id}",
            "subsystem": d.subsystem,
            "topic": d.topic,
            "decision": d.decision,
            "rationale": d.rationale,
            "alternatives_rejected": d.alternatives,
            "sources": d.sources,
            "confirmed": d.confirmed_at.date().isoformat() if d.confirmed_at else "",
            "related_to_this_task": d.id in linked,
        }
        for *_, d in ranked[:limit]
    ]


# --- confirm and supersede --------------------------------------------------


def confirm(
    session: Session,
    decision: Decision,
    lead: User,
    replaces: Decision | None = None,
) -> Decision:
    """Accept a proposed decision, optionally superseding a confirmed one.

    One transaction: either both decisions change or neither does. Raises
    DecisionError for a broken rule (the caller shows the message).
    """
    if lead["role"] != "lead":
        raise DecisionError("Only leads can confirm or supersede decisions.")
    if decision.status != "proposed":
        raise DecisionError("Only a proposed decision can be accepted.")
    if replaces is not None:
        if replaces.id == decision.id:
            raise DecisionError("A decision can't replace itself.")
        if replaces.status != "confirmed":
            raise DecisionError("Only a confirmed decision can be superseded.")

    now = utc_now()
    decision.status = "confirmed"
    decision.confirmed_by = lead["email"]
    decision.confirmed_at = now
    session.add(decision)
    if replaces is not None:
        replaces.status = "superseded"
        replaces.superseded_by_id = decision.id
        session.add(replaces)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return decision


# --- pages ------------------------------------------------------------------


def _form_page(
    request: Request,
    user: User,
    form: dict[str, Any],
    *,
    problems: list[str] | None = None,
    status: int = 200,
) -> Response:
    context = {
        "user": user,
        "form": form,
        "problems": problems or [],
        "subsystems": SUBSYSTEMS,
        "action": "/decisions",
    }
    return templates.TemplateResponse(
        request, "decision_form.html", context, status_code=status
    )


def _detail_page(
    request: Request,
    session: Session,
    user: User,
    decision_id: int,
    *,
    error: str | None = None,
    status: int | None = None,
) -> Response:
    decision: Decision | None = None
    page_error: str | None = None
    context: dict[str, Any] = {}
    try:
        decision = session.get(Decision, decision_id)
        if decision is not None:
            replaced_by = (
                session.get(Decision, decision.superseded_by_id)
                if decision.superseded_by_id
                else None
            )
            replaces = list(
                session.exec(
                    select(Decision).where(Decision.superseded_by_id == decision.id)
                ).all()
            )
            context = {
                "task_codes": task_codes_for(session, decision.id),
                "replaced_by": replaced_by,
                "replaces": replaces,
                "replaceable": _decisions_with_status(
                    session, "confirmed", decision.subsystem, decision.id
                )
                if decision.status == "proposed"
                else [],
                "replacements": _decisions_with_status(
                    session, "proposed", decision.subsystem, decision.id
                )
                if decision.status == "confirmed"
                else [],
            }
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Could not load decision (%s)", type(exc).__name__)
        page_error = "Couldn't load this decision from the database right now. Try again."

    not_found = decision is None and page_error is None
    if status is None:
        status = 503 if page_error else 404 if not_found else 200
    context.update(
        {
            "user": user,
            "decision": decision,
            "decision_id": decision_id,
            "status_labels": DECISION_STATUS_LABELS,
            "page_error": page_error,
            "not_found": not_found,
            "error": error,
        }
    )
    return templates.TemplateResponse(
        request, "decision.html", context, status_code=status
    )


def _decisions_with_status(
    session: Session, status: str, subsystem: str, exclude_id: int | None
) -> list[Decision]:
    found = session.exec(
        select(Decision)
        .where(Decision.status == status, Decision.subsystem == subsystem)
        .order_by(Decision.topic)
    ).all()
    return [d for d in found if d.id != exclude_id]


@router.get("/decisions", response_class=HTMLResponse)
def decision_list(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    subsystem: str = "",
    status: str = "",
):
    notices = []
    if subsystem and subsystem not in SUBSYSTEMS:
        notices.append(f"Unknown subsystem {subsystem!r}; showing every subsystem.")
        subsystem = ""
    if status and status not in DECISION_STATUSES:
        notices.append(f"Unknown status {status!r}; showing every status.")
        status = ""

    error = None
    decisions: list[Decision] = []
    try:
        statement = select(Decision)
        if subsystem:
            statement = statement.where(Decision.subsystem == subsystem)
        if status:
            statement = statement.where(Decision.status == status)
        found = session.exec(statement.order_by(col(Decision.created_at).desc())).all()
        decisions = sorted(found, key=lambda d: _STATUS_ORDER[d.status])
        total = len(session.exec(select(Decision.id)).all())
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Could not load decisions (%s)", type(exc).__name__)
        error = "Couldn't load decisions from the database right now. Try again in a moment."
        total = 0

    context = {
        "user": user,
        "decisions": decisions,
        "total_count": total,
        "subsystem": subsystem,
        "status": status,
        "subsystems": SUBSYSTEMS,
        "status_labels": DECISION_STATUS_LABELS,
        "notices": notices,
        "error": error,
    }
    return templates.TemplateResponse(
        request, "decisions.html", context, status_code=503 if error else 200
    )


@router.get("/decisions/new", response_class=HTMLResponse)
def new_decision_form(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    task: str = "",
    subsystem: str = "",
):
    form = blank_form(subsystem=subsystem if subsystem in SUBSYSTEMS else "")
    if task:
        try:
            found = session.exec(select(Task).where(Task.code == task.upper())).first()
        except Exception as exc:  # noqa: BLE001  (the form still works without the prefill)
            log.error("Could not look up task for decision form (%s)", type(exc).__name__)
            found = None
        if found is not None:
            form["tasks"] = found.code
            form["subsystem"] = found.subsystem
    return _form_page(request, user, form)


@router.post("/decisions", response_class=HTMLResponse)
async def propose_decision(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
) -> Response:
    form = read_form(await request.form())
    clean, problems = validate(form)

    tasks: list[Task] = []
    if not problems:
        try:
            tasks, missing = find_tasks(session, clean["task_codes"])
        except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
            log.error("Could not check task codes (%s)", type(exc).__name__)
            return _form_page(request, user, form, problems=[DB_ERROR], status=503)
        if missing:
            problems.append(f"No task has the code {', '.join(missing)}.")
    if problems:
        return _form_page(request, user, form, problems=problems, status=400)

    try:
        decision = create_decision(
            session, clean, tasks, user["email"], drafted=form["drafted"]
        )
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        session.rollback()
        log.error("Could not save decision (%s)", type(exc).__name__)
        return _form_page(request, user, form, problems=[DB_ERROR], status=503)
    return RedirectResponse(f"/decisions/{decision.id}", status_code=303)


@router.get("/decisions/{decision_id}", response_class=HTMLResponse)
def decision_detail(
    request: Request,
    decision_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
):
    return _detail_page(request, session, user, decision_id)


def _lead_action(
    request: Request,
    session: Session,
    lead: User,
    new_id: int,
    old_id: int | None,
    page_id: int,
) -> Response:
    """Accept decision `new_id`, optionally superseding `old_id`; show `page_id` after."""
    try:
        new = session.get(Decision, new_id)
        old = session.get(Decision, old_id) if old_id is not None else None
        if new is None or (old_id is not None and old is None):
            return _detail_page(
                request, session, lead, page_id, error="That decision no longer exists.", status=404
            )
        confirm(session, new, lead, replaces=old)
    except DecisionError as exc:
        return _detail_page(request, session, lead, page_id, error=str(exc), status=409)
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        session.rollback()
        log.error("Could not confirm decision (%s)", type(exc).__name__)
        return _detail_page(request, session, lead, page_id, error=DB_ERROR, status=503)
    return RedirectResponse(f"/decisions/{new_id}", status_code=303)


def _id_field(form: Mapping[str, Any], name: str) -> int | None:
    value = form.get(name)
    return int(value) if isinstance(value, str) and value.strip().isdigit() else None


@router.post("/decisions/{decision_id}/accept", response_class=HTMLResponse)
async def accept_decision(
    request: Request,
    decision_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    lead: LeadUser,
) -> Response:
    """Lead accepts a proposed decision; `replaces` (optional) is the one it supersedes."""
    form = await request.form()
    return _lead_action(
        request, session, lead, decision_id, _id_field(form, "replaces"), decision_id
    )


@router.post("/decisions/{decision_id}/supersede", response_class=HTMLResponse)
async def supersede_decision(
    request: Request,
    decision_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    lead: LeadUser,
) -> Response:
    """From the old, confirmed decision: `by` is the proposed one that replaces it."""
    form = await request.form()
    new_id = _id_field(form, "by")
    if new_id is None:
        return _detail_page(
            request, session, lead, decision_id,
            error="Choose the proposed decision that replaces this one.", status=400,
        )
    return _lead_action(request, session, lead, new_id, decision_id, decision_id)
