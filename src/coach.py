"""The coach: task page (/tasks/{code}) and the chat that posts to it.

The model is called only from `ask_coach`, only when a member sends a message,
never when a page loads. Model code itself lives in `llm.py`.

Access: every route here requires Google sign-in (`auth.CurrentUser`). The
signed-in member's verified email is the identity stored with each message and
model call, and the key for the daily cap.
"""

import json
import logging
import os
import re
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlmodel import Session, func, select

import db
import decisions
import llm
from auth import CurrentUser, User
from markup import render_markdown
from models import (
    DECISION_STATUS_LABELS,
    STATUS_LABELS,
    SUBSYSTEMS,
    TASK_DETAIL_LABELS,
    CoachMessage,
    LlmCall,
    Task,
    utc_now,
)

log = logging.getLogger("crush_coach")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()

DEFAULT_DAILY_LIMIT = 20
DEFAULT_TIMEZONE = "America/Los_Angeles"
HISTORY_TURNS = 10  # earlier messages sent to the model
MAX_MESSAGE_CHARS = 2000
MAX_CONTEXT_FIELD_CHARS = 1500  # per tracker field sent to the model

# Only Google links become clickable: the model's text can't plant other links.
# The one exception is this app's own decision pages, a relative path like /decisions/12
# that the model got from find_decisions.
_LINKABLE = re.compile(
    r"https://(?:docs|drive)\.google\.com/[^\s<]+|(?<![\w/])/decisions/\d+(?![\w/-])"
)


def linkify(text: str) -> Markup:
    """Escape text, then make Google Drive/Docs URLs and /decisions/N paths real links."""

    def link(match: re.Match[str]) -> str:
        url = match.group(0)
        if url.startswith("/"):  # a decision page: same site, same tab
            return f'<a href="{url}">{url}</a>'
        trailing = ""
        while url and url[-1] in ".,;:)!?":
            trailing = url[-1] + trailing
            url = url[:-1]
        return (
            f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'
            f"{trailing}"
        )

    return Markup(_LINKABLE.sub(link, str(escape(text))))


templates.env.filters["linkify"] = linkify
templates.env.filters["markdown"] = render_markdown

# What each member's in-flight coach answer is doing right now, for the progress poll.
# One entry per (member, task) while a question is being answered. It lives in this
# process only; if the poll can't see it the page just keeps its generic text.
_progress: dict[tuple[str, str], str] = {}


def _progress_key(member: str, code: str) -> tuple[str, str]:
    return (member.casefold(), code)


# --- configuration (read per request so tests and Railway env changes apply) ---


def daily_limit() -> int:
    try:
        return max(0, int(os.environ.get("COACH_DAILY_LIMIT") or DEFAULT_DAILY_LIMIT))
    except ValueError:
        return DEFAULT_DAILY_LIMIT


def _timezone() -> tzinfo:
    try:
        return ZoneInfo(os.environ.get("COACH_TIMEZONE") or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001  (bad name or no tz database: fall back to UTC)
        return UTC


def day_start_utc() -> datetime:
    """Start of the current local day, as naive UTC (how created_at is stored)."""
    local_now = datetime.now(_timezone())
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).replace(tzinfo=None)


# --- helpers ---


def task_details(task: Task) -> list[tuple[str, str]]:
    """(label, text) for each tracker field that has text, in display order."""
    found = []
    for name, label in TASK_DETAIL_LABELS.items():
        value = (getattr(task, name) or "").strip()
        if value:
            found.append((label, value))
    return found


def task_context(task: Task) -> dict[str, str]:
    """What the coach is told about the task. Tracker text is one line per field."""
    context = {
        "Code": task.code,
        "Title": task.title,
        "Subsystem": task.subsystem,
        "Type": task.task_type,
        "Stage": task.stage,
    }
    for label, value in task_details(task):
        flat = " ".join(value.split())
        if len(flat) > MAX_CONTEXT_FIELD_CHARS:
            flat = flat[:MAX_CONTEXT_FIELD_CHARS].rstrip() + "… (shortened)"
        context[label] = flat
    return context


def _view(message: CoachMessage, user: User) -> dict:
    meta = json.loads(message.meta) if message.meta else {}
    mine = message.member == user["email"]
    return {
        "role": message.role,
        "member": "You" if mine else message.member,
        "content": message.content,
        "sources": meta.get("sources", []),
        "notes": meta.get("notes", []),
    }


def _history(session: Session, task_id: int) -> list[CoachMessage]:
    return list(
        session.exec(
            select(CoachMessage)
            .where(CoachMessage.task_id == task_id)
            .order_by(CoachMessage.created_at, CoachMessage.id)
        ).all()
    )


def messages_today(session: Session, member: str) -> int:
    return session.exec(
        select(func.count())
        .select_from(CoachMessage)
        .where(
            CoachMessage.role == "user",
            func.lower(CoachMessage.member) == member.casefold(),
            CoachMessage.created_at >= day_start_utc(),
        )
    ).one()


def _render_page(
    request: Request,
    session: Session,
    user: User,
    code: str,
    *,
    error: str | None = None,
    draft: str = "",
) -> Response:
    task: Task | None = None
    messages: list[dict] = []
    page_error: str | None = None
    try:
        task = session.exec(select(Task).where(Task.code == code)).first()
        if task is not None:
            messages = [_view(m, user) for m in _history(session, task.id)]
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Could not load task page (%s)", type(exc).__name__)
        page_error = "Couldn't load this task from the database right now. Try again."

    # Decisions load on their own, so a failure here only affects that section.
    task_decisions: list = []
    decisions_error: str | None = None
    if task is not None:
        try:
            task_decisions = decisions.decisions_for_task(session, task.id)
        except Exception as exc:  # noqa: BLE001  (the rest of the page still works)
            log.error("Could not load task decisions (%s)", type(exc).__name__)
            decisions_error = "Couldn't load this task's decisions right now."

    if task is None and page_error is None:
        status_code = 404
    else:
        status_code = 503 if page_error else 200
    context = {
        "task": task,
        "code": code,
        "status_labels": STATUS_LABELS,
        "details": task_details(task) if task is not None else [],
        "messages": messages,
        "task_decisions": task_decisions,
        "decisions_error": decisions_error,
        "decision_status_labels": DECISION_STATUS_LABELS,
        "daily_limit": daily_limit(),
        "page_error": page_error,
        "not_found": task is None and page_error is None,
        "error": error,
        "draft": draft,
        "user": user,
    }
    return templates.TemplateResponse(
        request, "task.html", context, status_code=status_code
    )


def _add_llm_calls(
    session: Session, task_id: int, member: str, calls: list[llm.CallRecord]
) -> None:
    """Queue one `llm_call` row per model call (the caller commits)."""
    for call in calls:
        session.add(
            LlmCall(
                task_id=task_id,
                member=member,
                model=call.model,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                latency_ms=call.latency_ms,
                cost=call.cost,
                status=call.status,
                created_at=utc_now(),
            )
        )


def _fragment(
    request: Request,
    user: User,
    *,
    answer: dict | None = None,
    error: str | None = None,
) -> Response:
    """The htmx reply: the coach's answer or an error. The page already shows the
    member's own message (it adds it on submit), so it is not repeated here."""
    return templates.TemplateResponse(
        request,
        "_coach_exchange.html",
        {"answer": answer, "error": error, "user": user},
    )


# --- routes ---


@router.get("/tasks/{code}", response_class=HTMLResponse)
def task_page(
    request: Request,
    code: str,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
):
    return _render_page(request, session, user, code)


@router.get("/tasks/{code}/coach/progress")
def coach_progress(code: str, user: CurrentUser):
    """What the coach is doing for this member on this task ("" when idle)."""
    step = _progress.get(_progress_key(user["email"], code), "")
    return JSONResponse({"step": step}, headers={"Cache-Control": "no-store"})


@router.post("/tasks/{code}/coach", response_class=HTMLResponse)
def ask_coach(
    request: Request,
    code: str,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    message: Annotated[str, Form()] = "",
    hx_request: Annotated[str | None, Header()] = None,
):
    is_htmx = hx_request == "true"
    member = user["email"]
    message = message.strip()

    def fail(text: str) -> Response:
        """Show a plain error; the rest of the page keeps working."""
        if is_htmx:
            return _fragment(request, user, error=text)
        return _render_page(request, session, user, code, error=text, draft=message)

    if not message or len(message) > MAX_MESSAGE_CHARS:
        return fail(f"Write a message of up to {MAX_MESSAGE_CHARS} characters.")

    try:
        task = session.exec(select(Task).where(Task.code == code)).first()
        if task is None:
            raise HTTPException(status_code=404)

        limit = daily_limit()
        if messages_today(session, member) >= limit:
            return fail(
                f"You've used your {limit} coach messages for today. "
                "The count starts over at midnight."
            )

        history = [
            {"role": m.role, "content": m.content}
            for m in _history(session, task.id)[-HISTORY_TURNS:]
        ]
        # Saved before the call, so a failed attempt still counts toward the cap.
        session.add(
            CoachMessage(task_id=task.id, member=member, role="user", content=message)
        )
        session.commit()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Coach could not use the database (%s)", type(exc).__name__)
        return fail("Couldn't reach the database right now. Try again in a moment.")

    calls: list[llm.CallRecord] = []
    answer: llm.CoachAnswer | None = None
    llm_error: str | None = None
    task_id = task.id

    def lookup_decisions(query: str, subsystem: str) -> list[dict]:
        try:
            return decisions.search_confirmed(session, query, subsystem, task_id)
        except Exception:
            session.rollback()  # a failed query must not poison the later save
            raise

    progress_key = _progress_key(member, code)
    try:
        answer = llm.run_coach(
            task_context(task),
            history,
            message,
            record=calls.append,
            find_decisions=lookup_decisions,
            on_step=lambda step: _progress.__setitem__(progress_key, step),
        )
    except llm.LLMError as exc:
        llm_error = exc.message
    except Exception as exc:  # noqa: BLE001  (never let the model break the page)
        log.error("Coach failed unexpectedly (%s)", type(exc).__name__)
        llm_error = "The coach hit an unexpected problem. Try again in a moment."
    finally:
        _progress.pop(progress_key, None)

    notes: list[str] = []
    if answer is not None:
        if answer.drive_failed:
            notes.append(
                "Google Drive failed during this answer, so it may not use the "
                "team's documents."
            )
        if answer.decisions_failed:
            notes.append(
                "The team's decision records couldn't be checked for this answer, "
                "so it may disagree with a confirmed decision."
            )
        if answer.ungrounded_links:
            notes.append(
                "A link in the first draft of this answer did not come from a "
                "search, so it was removed."
            )
        if answer.unsourced_numbers:
            notes.append(
                "This answer includes numbers without a source. Don't machine "
                "from them; ask your lead."
            )

    try:
        _add_llm_calls(session, task_id, member, calls)
        if answer is not None:
            session.add(
                CoachMessage(
                    task_id=task_id,
                    member="Coach",
                    role="assistant",
                    content=answer.text,
                    meta=json.dumps({"sources": answer.sources, "notes": notes}),
                )
            )
        session.commit()
    except Exception as exc:  # noqa: BLE001  (the member still gets their answer)
        session.rollback()
        log.error("Coach could not save the exchange (%s)", type(exc).__name__)
        notes.append("This answer could not be saved to the task history.")

    if llm_error is not None:
        response = fail(llm_error)
    elif is_htmx:
        assert answer is not None
        response = _fragment(
            request,
            user,
            answer={"content": answer.text, "sources": answer.sources, "notes": notes},
        )
        # Tells coach.js to clear the box; on a failure the text stays for a retry.
        response.headers["HX-Trigger"] = "coachSent"
    else:
        response = RedirectResponse(f"/tasks/{code}", status_code=303)
    return response


# --- drafting a decision from the conversation ---

DRAFT_REQUEST_TEXT = "Draft a decision from this conversation."


def _draft_response(
    request: Request,
    user: User,
    is_htmx: bool,
    *,
    form: dict | None = None,
    error: str | None = None,
) -> Response:
    """The draft form (or a plain error) as a fragment for htmx, else a whole page."""
    context = {
        "user": user,
        "form": form,
        "problems": [],
        "error": error,
        "subsystems": SUBSYSTEMS,
        "action": "/decisions",
    }
    if is_htmx:
        return templates.TemplateResponse(request, "_decision_draft.html", context)
    if form is not None:
        return templates.TemplateResponse(request, "decision_form.html", context)
    return templates.TemplateResponse(
        request, "decision_form.html", {**context, "form": decisions.blank_form()}
    )


@router.post("/tasks/{code}/decision-draft", response_class=HTMLResponse)
def draft_decision(
    request: Request,
    code: str,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    hx_request: Annotated[str | None, Header()] = None,
):
    """The coach drafts a decision from this task's conversation.

    Nothing is saved as a decision here. The member gets a pre-filled form, reviews
    it, and submits it as a proposal; a lead then accepts or not. The draft counts as
    one coach message toward the daily cap.
    """
    is_htmx = hx_request == "true"
    member = user["email"]

    def fail(text: str) -> Response:
        return _draft_response(request, user, is_htmx, error=text)

    try:
        task = session.exec(select(Task).where(Task.code == code)).first()
        if task is None:
            raise HTTPException(status_code=404)
        task_id = task.id
        thread = _history(session, task_id)
        if not any(m.role == "assistant" for m in thread):
            return fail(
                "There is nothing to draft from yet. Ask the coach about this "
                "decision first, then draft it."
            )
        limit = daily_limit()
        if messages_today(session, member) >= limit:
            return fail(
                f"You've used your {limit} coach messages for today. "
                "The count starts over at midnight."
            )
        history = [
            {"role": m.role, "content": m.content} for m in thread[-HISTORY_TURNS:]
        ]
        # Saved before the call, like a chat message, so a failed attempt still counts.
        session.add(
            CoachMessage(
                task_id=task_id, member=member, role="user", content=DRAFT_REQUEST_TEXT
            )
        )
        session.commit()
        context = task_context(task)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Decision draft could not use the database (%s)", type(exc).__name__)
        return fail("Couldn't reach the database right now. Try again in a moment.")

    calls: list[llm.CallRecord] = []
    draft: dict | None = None
    llm_error: str | None = None
    try:
        draft = llm.draft_decision(context, history, record=calls.append)
    except llm.LLMError as exc:
        llm_error = exc.message
    except Exception as exc:  # noqa: BLE001  (never let the model break the page)
        log.error("Decision draft failed unexpectedly (%s)", type(exc).__name__)
        llm_error = "The coach hit an unexpected problem. Try again in a moment."

    form = decisions.form_from_draft(draft, task) if draft is not None else None
    try:
        _add_llm_calls(session, task_id, member, calls)
        if form is not None:
            topic = form["topic"] or "untitled"
            session.add(
                CoachMessage(
                    task_id=task_id,
                    member="Coach",
                    role="assistant",
                    content=(
                        f"Drafted a decision: {topic}. Review it, edit anything "
                        "that's wrong, and submit it for a lead to accept."
                    ),
                    meta=json.dumps({"sources": [], "notes": []}),
                )
            )
        session.commit()
    except Exception as exc:  # noqa: BLE001  (the member still gets their draft)
        session.rollback()
        log.error("Decision draft could not be logged (%s)", type(exc).__name__)

    if form is None:
        return fail(llm_error or "The coach couldn't draft a decision. Try again.")
    return _draft_response(request, user, is_htmx, form=form)
