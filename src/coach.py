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
from collections.abc import Iterator
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlmodel import Session, func, select

import answering
import db
import decisions
import llm
import retrieval
import taskpage
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
templates.env.filters["message_parts"] = taskpage.message_parts  # presentation only

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


def _meta(message: CoachMessage) -> dict:
    try:
        meta = json.loads(message.meta) if message.meta else {}
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _view(message: CoachMessage, user: User, retry_id: int | None = None) -> dict:
    meta = _meta(message)
    if message.role == "error":
        return {
            "role": "error",
            "member": "Coach",
            "content": message.content,
            "sources": [],
            "notes": [],
            "retry_id": retry_id,
        }
    mine = message.member == user["email"]
    return {
        "role": message.role,
        "member": "You" if mine else message.member,
        "content": message.content,
        "sources": meta.get("sources", []),
        "notes": meta.get("notes", []),
    }


def _views(messages: list[CoachMessage], user: User) -> list[dict]:
    """The thread as the page shows it. A failure gets a Retry button only while it is
    the last thing in the thread: after that the member has moved on."""
    views = []
    for position, message in enumerate(messages):
        retry_id = None
        if message.role == "error" and position == len(messages) - 1:
            retry_id = _meta(message).get("retry_of")
        views.append(_view(message, user, retry_id))
    return views


def _history(session: Session, task_id: int) -> list[CoachMessage]:
    return list(
        session.exec(
            select(CoachMessage)
            .where(CoachMessage.task_id == task_id)
            .order_by(CoachMessage.created_at, CoachMessage.id)
        ).all()
    )


def _chat(
    thread: list[CoachMessage], skip: set[int] | None = None
) -> list[CoachMessage]:
    """The turns the model is shown: member messages and coach answers. A message whose
    answer failed was never answered, so it is left out (and so is the failure)."""
    failed = {_meta(m).get("retry_of") for m in thread if m.role == "error"}
    left_out = failed | (skip or set())
    return [
        m for m in thread if m.role in ("user", "assistant") and m.id not in left_out
    ]


def _model_history(chat: list[CoachMessage]) -> list[dict[str, str]]:
    turns = [{"role": m.role, "content": m.content} for m in chat][-HISTORY_TURNS:]
    while turns and turns[0]["role"] != "user":
        turns.pop(0)  # the model's first turn must be the member's
    return turns


def _earlier_sources(chat: list[CoachMessage]) -> list[dict[str, Any]]:
    """Sources the coach cited earlier in this thread, newest answer first.

    Kept for the whole thread, not only the turns the model is shown, so a follow-up
    still has the documents the first answer used.
    """
    found: dict[str, dict[str, Any]] = {}
    for message in reversed(chat):
        if message.role != "assistant":
            continue
        for source in _meta(message).get("sources", []):
            link = source.get("link") if isinstance(source, dict) else None
            if link and link not in found:
                found[link] = source
    return list(found.values())[: retrieval.EARLIER_SOURCES * 2]


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
            messages = _views(_history(session, task.id), user)
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
    if task is not None and page_error is None:
        context.update(taskpage.page_context(session, task, user))  # presentation only
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


def _exchange_html(code: str, **context: Any) -> str:
    """The coach's reply as an HTML fragment (the `_coach_exchange.html` partial)."""
    return templates.get_template("_coach_exchange.html").render(code=code, **context)


def _fragment(
    request: Request,
    code: str,
    user: User,
    *,
    answer: dict | None = None,
    failure: dict | None = None,
    error: str | None = None,
) -> Response:
    """The htmx reply: the coach's answer, a failure with Retry, or a plain notice. The
    page already shows the member's own message, so it is not repeated here."""
    return templates.TemplateResponse(
        request,
        "_coach_exchange.html",
        {
            "code": code,
            "answer": answer,
            "failure": failure,
            "error": error,
            "user": user,
        },
    )


# --- one message, start to finish ---

NO_INDEX = "Drive index is empty: ask a lead to sync."
DB_DOWN = "Couldn't reach the database right now. Try again in a moment."
UNEXPECTED = "The coach hit an unexpected problem. Try again in a moment."
DROPPED = "The connection dropped before the answer finished."


def today() -> str:
    return datetime.now(_timezone()).date().isoformat()


def _save_failure(
    task_id: int,
    member: str,
    user_message_id: int | None,
    text: str,
    calls: list[llm.CallRecord],
) -> None:
    """Keep what failed in the thread (with the call costs) so a reload still shows it
    with a Retry button. Never raises: the failure is shown either way."""
    try:
        with Session(db.engine) as session:
            _add_llm_calls(session, task_id, member, calls)
            if user_message_id is not None:
                session.add(
                    CoachMessage(
                        task_id=task_id,
                        member="Coach",
                        role="error",
                        content=text,
                        meta=json.dumps({"retry_of": user_message_id}),
                    )
                )
            session.commit()
    except Exception as exc:  # noqa: BLE001
        log.error("Coach could not save a failure (%s)", type(exc).__name__)


def converse(
    code: str, user: User, message: str, retry_of: int | None = None
) -> Iterator[dict[str, Any]]:
    """Answer one message from a member, yielding what the page needs as it happens.

    Events: `step` (text), `text` (the answer so far, Markdown), `reset`, then exactly
    one of `done` (answer: content, sources, notes), `failed` (message, retry_id: the
    member's message is saved and can be retried) or `rejected` (message: nothing was
    saved, so the text stays in the box).

    The order is: save the member's message (so it is never lost and counts toward the
    cap), retrieve with no model call, one streamed model call, save the answer. With
    `retry_of`, the member's earlier message is answered again instead of a new one.
    """
    member = user["email"]
    message = message.strip()
    calls: list[llm.CallRecord] = []
    user_message_id: int | None = None
    task_id: int | None = None

    def failed(text: str) -> dict[str, Any]:
        if task_id is not None:
            _save_failure(task_id, member, user_message_id, text, calls)
        return {"type": "failed", "message": text, "retry_id": user_message_id}

    try:
        with Session(db.engine) as session:
            task = session.exec(select(Task).where(Task.code == code)).first()
            if task is None:
                yield {"type": "rejected", "message": f"There is no task “{code}”."}
                return
            task_id = task.id
            thread = _history(session, task_id)
            if retry_of is not None:
                original = next((m for m in thread if m.id == retry_of), None)
                if (
                    original is None
                    or original.role != "user"
                    or original.member.casefold() != member.casefold()
                ):
                    yield {
                        "type": "rejected",
                        "message": "That message is gone. Write it again.",
                    }
                    return
                if any(m.role == "assistant" and m.id > original.id for m in thread):
                    yield {
                        "type": "rejected",
                        "message": "The coach already answered that message.",
                    }
                    return
                message = original.content
                user_message_id = original.id
                for old in thread:  # the failure being retried goes away
                    if old.role == "error" and _meta(old).get("retry_of") == retry_of:
                        session.delete(old)
                session.commit()
                thread = _history(session, task_id)
            else:
                if not message or len(message) > MAX_MESSAGE_CHARS:
                    yield {
                        "type": "rejected",
                        "message": f"Write a message of up to {MAX_MESSAGE_CHARS} characters.",
                    }
                    return
                limit = daily_limit()
                if messages_today(session, member) >= limit:
                    yield {
                        "type": "rejected",
                        "message": f"You've used your {limit} coach messages for today. "
                        "The count starts over at midnight.",
                    }
                    return
                # Saved before anything can fail, so the message is never lost and a
                # failed attempt still counts toward the cap.
                row = CoachMessage(
                    task_id=task_id, member=member, role="user", content=message
                )
                session.add(row)
                session.commit()
                user_message_id = row.id
                thread = _history(session, task_id)

            chat = _chat(thread, skip={user_message_id})
            history = _model_history(chat)
            previous = next((m.content for m in reversed(chat) if m.role == "user"), "")
            context = task_context(task)
            yield {"type": "step", "text": "Searching the Drive index…"}
            found = retrieval.retrieve(
                session,
                task,
                message,
                previous_question=previous,
                earlier_sources=_earlier_sources(chat),
            )
    except retrieval.IndexEmpty:
        yield failed(NO_INDEX)
        return
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Coach could not use the database (%s)", type(exc).__name__)
        if user_message_id is None:
            yield {"type": "rejected", "message": DB_DOWN}
        else:
            yield failed(DB_DOWN)
        return

    count = len(found.chunks) + len(found.earlier)
    yield {
        "type": "step",
        "text": f"Found {count} passage{'' if count == 1 else 's'} in the Drive index. "
        "Writing the answer…",
    }
    run = answering.AnswerRun(
        context, found, message, history, today=today(), record=calls.append
    )
    events = iter(run)
    try:
        yield from events
    except GeneratorExit:  # the page went away mid-answer
        events.close()
        _save_failure(task_id, member, user_message_id, DROPPED, calls)
        raise
    except llm.LLMError as exc:
        yield failed(exc.message)
        return
    except Exception as exc:  # noqa: BLE001  (never let the model break the page)
        log.error("Coach failed unexpectedly (%s)", type(exc).__name__)
        yield failed(UNEXPECTED)
        return

    answer = run.answer
    assert answer is not None
    notes = answering.notes_for(answer)
    try:
        with Session(db.engine) as session:
            _add_llm_calls(session, task_id, member, calls)
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
        log.error("Coach could not save the exchange (%s)", type(exc).__name__)
        notes.append("This answer could not be saved to the task history.")
    log.info(
        "Coach answered %s in %.1fs (first text %.1fs, retrieval %.2fs, %d reads)",
        code,
        answer.seconds,
        answer.first_text_seconds or 0.0,
        found.seconds,
        answer.tool_calls,
    )
    yield {
        "type": "done",
        "answer": {"content": answer.text, "sources": answer.sources, "notes": notes},
    }


# --- routes ---


@router.get("/tasks/{code}", response_class=HTMLResponse)
def task_page(
    request: Request,
    code: str,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
):
    return _render_page(request, session, user, code)


def _retry_id(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


@router.post("/tasks/{code}/coach/stream")
def stream_coach(
    code: str,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    message: Annotated[str, Form()] = "",
    retry: Annotated[str, Form()] = "",
):
    """The coach's answer as it is written: one JSON object per line.

    `step` and `reset` pass through; `text` carries the answer so far already rendered
    as safe HTML; the last line is `done` or `failed` (an HTML fragment for the thread)
    or `rejected` (a plain message, nothing saved). The page (`coach.js`) shows the
    lines as they arrive.
    """
    if session.exec(select(Task.id).where(Task.code == code)).first() is None:
        raise HTTPException(status_code=404)

    def lines() -> Iterator[str]:
        for event in converse(code, user, message, _retry_id(retry)):
            kind = event["type"]
            if kind == "text":
                out = {"type": "text", "html": str(render_markdown(event["text"]))}
            elif kind == "done":
                html = _exchange_html(code, answer=event["answer"], user=user)
                out = {"type": "done", "html": html}
            elif kind == "failed":
                failure = {"content": event["message"], "retry_id": event["retry_id"]}
                out = {
                    "type": "failed",
                    "html": _exchange_html(code, failure=failure, user=user),
                }
            else:
                out = event
            yield json.dumps(out) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/tasks/{code}/coach", response_class=HTMLResponse)
def ask_coach(
    request: Request,
    code: str,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    message: Annotated[str, Form()] = "",
    retry: Annotated[str, Form()] = "",
    hx_request: Annotated[str | None, Header()] = None,
):
    """The same answer without streaming: a plain form post (no JavaScript) or htmx."""
    is_htmx = hx_request == "true"
    message = message.strip()
    if session.exec(select(Task.id).where(Task.code == code)).first() is None:
        raise HTTPException(status_code=404)

    last: dict[str, Any] = {}
    for event in converse(code, user, message, _retry_id(retry)):
        if event["type"] in ("done", "failed", "rejected"):
            last = event

    if last.get("type") == "rejected":
        if is_htmx:
            return _fragment(request, code, user, error=last["message"])
        return _render_page(
            request, session, user, code, error=last["message"], draft=message
        )
    if is_htmx:
        if last["type"] == "done":
            response = _fragment(request, code, user, answer=last["answer"])
            # Tells coach.js to clear the box; on a failure the text stays.
            response.headers["HX-Trigger"] = "coachSent"
            return response
        failure = {"content": last["message"], "retry_id": last["retry_id"]}
        return _fragment(request, code, user, failure=failure)
    return RedirectResponse(f"/tasks/{code}", status_code=303)


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
        thread = _chat(_history(session, task_id))
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
