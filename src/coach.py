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
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlmodel import Session, func, select

import db
import llm
from auth import CurrentUser, User
from models import STATUS_LABELS, CoachMessage, LlmCall, Task, utc_now

log = logging.getLogger("crush_coach")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()

DEFAULT_DAILY_LIMIT = 20
DEFAULT_TIMEZONE = "America/Los_Angeles"
HISTORY_TURNS = 10  # earlier messages sent to the model
MAX_MESSAGE_CHARS = 2000

# Only Google links become clickable: the model's text can't plant other links.
_GOOGLE_URL = re.compile(r"https://(?:docs|drive)\.google\.com/[^\s<]+")


def linkify(text: str) -> Markup:
    """Escape text, then make Google Drive/Docs URLs real links."""

    def link(match: re.Match[str]) -> str:
        url = match.group(0)
        trailing = ""
        while url and url[-1] in ".,;:)!?":
            trailing = url[-1] + trailing
            url = url[:-1]
        return (
            f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'
            f"{trailing}"
        )

    return Markup(_GOOGLE_URL.sub(link, str(escape(text))))


templates.env.filters["linkify"] = linkify


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

    if task is None and page_error is None:
        status_code = 404
    else:
        status_code = 503 if page_error else 200
    context = {
        "task": task,
        "code": code,
        "status_labels": STATUS_LABELS,
        "messages": messages,
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


def _fragment(
    request: Request,
    user: User,
    *,
    user_text: str | None = None,
    answer: dict | None = None,
    error: str | None = None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "_coach_exchange.html",
        {"user_text": user_text, "answer": answer, "error": error, "user": user},
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

    def fail(text: str, *, user_text: str | None = None) -> Response:
        """Show a plain error; the rest of the page keeps working."""
        if is_htmx:
            return _fragment(request, user, user_text=user_text, error=text)
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
    try:
        answer = llm.run_coach(
            {
                "Code": task.code,
                "Title": task.title,
                "Subsystem": task.subsystem,
                "Type": task.task_type,
                "Stage": task.stage,
            },
            history,
            message,
            record=calls.append,
        )
    except llm.LLMError as exc:
        llm_error = exc.message
    except Exception as exc:  # noqa: BLE001  (never let the model break the page)
        log.error("Coach failed unexpectedly (%s)", type(exc).__name__)
        llm_error = "The coach hit an unexpected problem. Try again in a moment."

    notes: list[str] = []
    if answer is not None:
        if answer.drive_failed:
            notes.append(
                "Google Drive failed during this answer, so it may not use the "
                "team's documents."
            )
        if answer.unsourced_numbers:
            notes.append(
                "This answer includes numbers without a source. Don't machine "
                "from them; ask your lead."
            )

    try:
        for call in calls:
            session.add(
                LlmCall(
                    task_id=task.id,
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
        if answer is not None:
            session.add(
                CoachMessage(
                    task_id=task.id,
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
        response = fail(llm_error, user_text=message)
    elif is_htmx:
        assert answer is not None
        response = _fragment(
            request,
            user,
            user_text=message,
            answer={"content": answer.text, "sources": answer.sources, "notes": notes},
        )
        # Tells coach.js to clear the box; on a failure the text stays for a retry.
        response.headers["HX-Trigger"] = "coachSent"
    else:
        response = RedirectResponse(f"/tasks/{code}", status_code=303)
    return response
