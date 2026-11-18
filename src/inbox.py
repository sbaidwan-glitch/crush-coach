"""The Inbox (`/inbox`): everything waiting on a lead, in one list called "Needs you".

Three kinds of card, each with its own mark:

- **Work to check**: a task waiting at a review step. Accept or Send back (the moves
  themselves are `workflow.move`); on Accept an optional line is saved as a recorded
  answer for the task's subsystem.
- **Answer to settle** (`AnswerRequest`): a member pressed "Ask a lead to settle this"
  on a coach answer. A lead records one of the options, another value, or typed text.
- **New task** (`TaskSuggestion`): a member's "Suggest a task". A lead adds it (maybe
  after editing it) or dismisses it.

Any lead can act, nobody else can: every route here checks the role itself, the page
only hides the buttons. Nothing a lead must act on lives anywhere else. Nothing here
calls the model.
"""

import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, col, func, select

import board
import db
import decisions
import import_tasks
import shell
import steps
import taskpage
from auth import CurrentUser, LeadUser, User
from markup import render_markdown
from models import (
    SUBSYSTEMS,
    TASK_TYPES,
    AnswerRequest,
    CoachMessage,
    Task,
    TaskSuggestion,
    utc_now,
)
from progress import owned_by

log = logging.getLogger("crush_coach")
templates = shell.templates()
templates.env.filters["markdown"] = render_markdown  # a coach answer on an Answer to settle
router = APIRouter()

MAX_TITLE_CHARS = 200
MAX_WHY_CHARS = 1000
MAX_ANSWER_CHARS = 500
MAX_OPTIONS = 4
DB_ERROR = "Couldn't reach the database right now. Nothing was changed. Try again."
# The first letter of a new task's code, by subsystem (docs: M mechanical, E electronics
# mounts, G gripper, T torpedo).
CODE_PREFIX = {"Electronics Mounts": "E", "Gripper": "G", "Torpedo": "T"}
_GOOGLE_LINK = re.compile(r"https://(?:docs|drive)\.google\.com/[^\s<>)\]]+")


class InboxError(Exception):
    """A move that isn't allowed or has no effect. The message is safe to show; `status`
    is the HTTP code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# --- what is waiting -------------------------------------------------------------


def count(session: Session) -> int:
    """How many cards a lead has: the number on the Inbox link."""
    work = session.exec(
        select(func.count()).select_from(Task).where(Task.status == "needs_review")
    ).one()
    answers = session.exec(
        select(func.count()).select_from(AnswerRequest).where(AnswerRequest.status == "open")
    ).one()
    new_tasks = session.exec(
        select(func.count()).select_from(TaskSuggestion).where(TaskSuggestion.status == "open")
    ).one()
    return work + answers + new_tasks


def work_cards(
    session: Session, tasks: list[Task], lead: User | None = None
) -> list[dict[str, Any]]:
    """Tasks waiting at a review step, longest wait first. `mine` marks the lead's own."""
    cards = []
    for row in board.waiting_for_review(session, tasks):
        task = row["task"]
        found = _GOOGLE_LINK.search(task.documentation or "")
        listed = steps.steps_for(task)
        here = steps.current_index(task, listed)
        cards.append(
            {
                "kind": "work",
                "id": task.id,
                "task": task,
                "step": task.stage,
                "finished": listed[here - 1].name if 0 < here <= len(listed) else None,
                "mine": lead is not None and owned_by(task, lead["name"], lead["email"]),
                "age": row["age"],
                "docs_link": found.group(0) if found else None,
            }
        )
    return cards


def answer_cards(session: Session) -> list[dict[str, Any]]:
    """Answers to settle, oldest first; the ones a lead said "Not now" to go last."""
    found = session.exec(
        select(AnswerRequest)
        .where(AnswerRequest.status == "open")
        .order_by(col(AnswerRequest.created_at), col(AnswerRequest.id))
    ).all()
    found = sorted(found, key=lambda r: r.deferred_at is not None)
    return [{"kind": "answer", "id": r.id, "request": r} for r in found]


def task_cards(session: Session, edit_id: int | None = None) -> list[dict[str, Any]]:
    """Suggested tasks, oldest first. A card is shown as a form when it is being edited,
    or when its subsystem can't be guessed (a lead has to choose one)."""
    found = session.exec(
        select(TaskSuggestion)
        .where(TaskSuggestion.status == "open")
        .order_by(col(TaskSuggestion.created_at), col(TaskSuggestion.id))
    ).all()
    cards = []
    for s in found:
        subsystem = import_tasks.guess_subsystem(s.title)
        cards.append(
            {
                "kind": "task",
                "id": s.id,
                "suggestion": s,
                "subsystem": subsystem,
                "task_type": import_tasks.guess_task_type(s.title),
                "editing": s.id == edit_id or subsystem is None,
            }
        )
    return cards


def load(
    session: Session, tasks: list[Task], edit_id: int | None = None, lead: User | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Every card, work first, then answers, then new tasks, and a plain sentence for
    each kind that couldn't be loaded (the rest still shows)."""
    cards: list[dict[str, Any]] = []
    errors: list[str] = []
    for label, loader in (
        ("work to check", lambda: work_cards(session, tasks, lead)),
        ("answers to settle", lambda: answer_cards(session)),
        ("suggested tasks", lambda: task_cards(session, edit_id)),
    ):
        try:
            cards += loader()
        except Exception as exc:  # noqa: BLE001  (one kind failing must not hide the others)
            session.rollback()
            log.error("Could not load %s (%s)", label, type(exc).__name__)
            errors.append(f"Couldn't load the {label} right now.")
    return cards, errors


# --- a member asks a lead to settle an answer ---------------------------------------


def asks_for_a_lead(content: str, sources: list[Any]) -> bool:
    """Whether a coach answer is one a member can hand to a lead: its sources conflict
    (the "Likely current" paragraph), or it cites nothing, so nothing answers it."""
    parts = taskpage.message_parts(content)
    return any(p["kind"] == "conflict" for p in parts) or not sources


def open_request_for(session: Session, message_id: int) -> AnswerRequest | None:
    return session.exec(
        select(AnswerRequest).where(
            AnswerRequest.message_id == message_id, AnswerRequest.status == "open"
        )
    ).first()


def create_answer_request(
    session: Session,
    task: Task,
    answer: CoachMessage,
    question: str,
    member: User,
    sources: list[dict[str, Any]],
    options: list[dict[str, Any]] | None = None,
    suggested: int | None = None,
) -> AnswerRequest:
    """Put a coach answer in front of the leads. Asking twice about the same answer
    returns the request that is already open."""
    if answer.role != "assistant" or answer.task_id != task.id:
        raise InboxError("That isn't one of this task's coach answers.", 404)
    existing = open_request_for(session, answer.id)
    if existing is not None:
        return existing
    conflict = any(p["kind"] == "conflict" for p in taskpage.message_parts(answer.content))
    options = (options or [])[:MAX_OPTIONS]
    request = AnswerRequest(
        task_id=task.id,
        task_code=task.code,
        message_id=answer.id,
        subsystem=task.subsystem,
        question=question.strip()[:MAX_TITLE_CHARS * 2],
        asked_by=member["name"],
        kind="conflict" if conflict else "none",
        answer_text=answer.content,
        options=options,
        sources=sources,
        suggested=suggested if suggested is not None and suggested < len(options) else None,
        suggestion=taskpage.next_step_from(answer.content),
    )
    session.add(request)
    session.commit()
    return request


# --- a lead settles an answer ----------------------------------------------------


def _need_lead(lead: User, what: str) -> None:
    if lead["role"] != "lead":
        raise InboxError(f"Only a lead can {what}.", 403)


def record_request(
    session: Session, lead: User, request_id: int, choice: str = "", typed: str = ""
) -> AnswerRequest:
    """Record the lead's answer: the option they chose (`choice` is its number), or the
    text they typed. Typed text wins over a ticked option, since typing is deliberate.
    Makes the recorded answer and closes the request in one commit."""
    _need_lead(lead, "record an answer")
    req = session.get(AnswerRequest, request_id)
    if req is None:
        raise InboxError("That question isn't there any more.", 404)
    if req.status != "open":
        raise InboxError(
            "Someone has already recorded an answer to this. Reload the page.", 409
        )
    typed = typed.strip()[:MAX_ANSWER_CHARS]
    source: dict[str, Any] | None = None
    if typed:
        value = typed
    elif choice.isdigit() and int(choice) < len(req.options):
        option = req.options[int(choice)]
        value = str(option.get("value") or "").strip()
        source = option
    else:
        raise InboxError("Choose an answer or type one, then record it.")
    if not value:
        raise InboxError("Choose an answer or type one, then record it.")

    why = ""
    link = None
    if source is not None:
        title, date = source.get("source") or "", source.get("date") or ""
        why = f"From {title}{', ' + date if date else ''}." if title else ""
        link = source.get("link") or None
    task = session.get(Task, req.task_id) if req.task_id else None
    decision = decisions.record_answer(
        session,
        lead,
        subsystem=req.subsystem,
        topic=req.question,
        answer=value,
        why=why,
        task=task,
        sources=[{"title": source["source"], "link": link, "folder": "", "year": None}]
        if source is not None and link and source.get("source")
        else [],
        replace_same_topic=True,
    )
    req.status = "recorded"
    req.settled_by = lead["name"]
    req.settled_at = utc_now()
    req.decision_id = decision.id
    session.add(req)
    session.commit()
    decisions.write_to_drive(session, req.decision_id)  # after the commit: it is saved anyway
    return req


def defer_request(session: Session, lead: User, request_id: int) -> None:
    """"Not now": the question stays open and moves to the end of the list."""
    _need_lead(lead, "settle an answer")
    req = session.get(AnswerRequest, request_id)
    if req is None or req.status != "open":
        raise InboxError("That question isn't waiting any more. Reload the page.", 409)
    req.deferred_at = utc_now()
    session.add(req)
    session.commit()


# --- suggested tasks -------------------------------------------------------------


def suggest_task(session: Session, member: User, title: str, why: str) -> TaskSuggestion:
    title, why = " ".join(title.split()), why.strip()
    if not title:
        raise InboxError("Give the task a title.")
    if len(title) > MAX_TITLE_CHARS:
        raise InboxError(f"The title is too long (up to {MAX_TITLE_CHARS} characters).")
    if len(why) > MAX_WHY_CHARS:
        raise InboxError(f"“Why” is too long (up to {MAX_WHY_CHARS} characters).")
    suggestion = TaskSuggestion(title=title, why=why, suggested_by=member["name"])
    session.add(suggestion)
    session.commit()
    return suggestion


def _open_suggestion(session: Session, suggestion_id: int) -> TaskSuggestion:
    found = session.get(TaskSuggestion, suggestion_id)
    if found is None:
        raise InboxError("That suggestion isn't there any more.", 404)
    if found.status != "open":
        raise InboxError("Someone has already dealt with this suggestion. Reload the page.", 409)
    return found


def next_code(session: Session, subsystem: str) -> str:
    taken = set(session.exec(select(Task.code)).all())
    prefix = CODE_PREFIX.get(subsystem, "M")
    return import_tasks.next_free_code(f"{prefix}00", taken) or f"{prefix}01"


def add_task(
    session: Session,
    lead: User,
    suggestion_id: int,
    title: str,
    subsystem: str,
    task_type: str,
    why: str,
) -> Task:
    """Make a task from a suggestion (with whatever the lead edited) and close it."""
    _need_lead(lead, "add a task")
    suggestion = _open_suggestion(session, suggestion_id)
    title, why = " ".join(title.split()), why.strip()
    if not title or len(title) > MAX_TITLE_CHARS:
        raise InboxError(f"Give the task a title of up to {MAX_TITLE_CHARS} characters.")
    if subsystem not in SUBSYSTEMS:
        raise InboxError("Choose a subsystem for the task (press Edit).")
    if task_type not in TASK_TYPES:
        raise InboxError("Choose a task type (press Edit).")
    task = Task(
        code=next_code(session, subsystem),
        title=title,
        subsystem=subsystem,
        task_type=task_type,
        stage=steps.fit_stage(task_type, "todo", ""),
        status="todo",
        description=why or None,
    )
    suggestion.status = "added"
    suggestion.resolved_by = lead["name"]
    suggestion.resolved_at = utc_now()
    suggestion.task_code = task.code
    session.add(task)
    session.add(suggestion)
    session.commit()
    return task


def dismiss_task(session: Session, lead: User, suggestion_id: int) -> None:
    _need_lead(lead, "dismiss a suggestion")
    suggestion = _open_suggestion(session, suggestion_id)
    suggestion.status = "dismissed"
    suggestion.resolved_by = lead["name"]
    suggestion.resolved_at = utc_now()
    session.add(suggestion)
    session.commit()


# --- routes ----------------------------------------------------------------------


def render_inbox(
    request: Request,
    session: Session,
    user: User,
    *,
    notice: str | None = None,
    status: int = 200,
    edit: int | None = None,
) -> HTMLResponse:
    """The Inbox page. `notice` says what went wrong with a lead's action on a card (the
    rest of the page still shows); `edit` opens a suggested task for editing."""
    tasks: list[Task] = []
    cards: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        tasks = board.load_tasks(session)
    except Exception as exc:  # noqa: BLE001  (say so; the sidebar and notice still show)
        session.rollback()
        log.error("Could not load tasks for the inbox (%s)", type(exc).__name__)
        errors = ["Couldn't load what is waiting for you right now."]
    if not errors:
        cards, errors = load(session, tasks, edit, user)
    context = {
        "user": user,
        "cards": cards,
        "errors": errors,
        "notice": notice,
        "subsystems": SUBSYSTEMS,
        "task_types": TASK_TYPES,
    }
    return templates.TemplateResponse(request, "inbox.html", context, status_code=status)


@router.get("/inbox", response_class=HTMLResponse)
def inbox_page(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    lead: LeadUser,
    edit: int | None = None,
):
    return render_inbox(request, session, lead, edit=edit)


def _back(request: Request, session: Session, user: User, exc: Exception) -> Response:
    """The Inbox again, saying what went wrong; the rest of the page still works."""
    session.rollback()
    if isinstance(exc, InboxError):
        message, status = exc.message, exc.status
    else:
        log.error("Could not save a lead's action (%s)", type(exc).__name__)
        message, status = DB_ERROR, 503
    return render_inbox(request, session, user, notice=message, status=status)


@router.post("/answers/{request_id}/record", response_class=Response)
def record_answer(
    request: Request,
    request_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    lead: LeadUser,
    choice: Annotated[str, Form()] = "",
    typed: Annotated[str, Form()] = "",
):
    try:
        record_request(session, lead, request_id, choice, typed)
    except Exception as exc:  # noqa: BLE001  (the page says what failed)
        return _back(request, session, lead, exc)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/answers/{request_id}/later", response_class=Response)
def answer_later(
    request: Request,
    request_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    lead: LeadUser,
):
    try:
        defer_request(session, lead, request_id)
    except Exception as exc:  # noqa: BLE001
        return _back(request, session, lead, exc)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/task-suggestions", response_class=Response)
def suggest(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    title: Annotated[str, Form()] = "",
    why: Annotated[str, Form()] = "",
):
    try:
        suggest_task(session, user, title, why)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        if isinstance(exc, InboxError):
            message, status = exc.message, exc.status
        else:
            log.error("Could not save a task suggestion (%s)", type(exc).__name__)
            message, status = DB_ERROR, 503
        return board.render_board(
            request, session, user, notice=message, status=status, draft=(title, why)
        )
    return RedirectResponse("/tasks?suggested=1#suggest", status_code=303)


@router.post("/task-suggestions/{suggestion_id}/add", response_class=Response)
def add(
    request: Request,
    suggestion_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    lead: LeadUser,
    title: Annotated[str, Form()] = "",
    subsystem: Annotated[str, Form()] = "",
    task_type: Annotated[str, Form()] = "",
    why: Annotated[str, Form()] = "",
):
    try:
        add_task(session, lead, suggestion_id, title, subsystem, task_type, why)
    except Exception as exc:  # noqa: BLE001
        return _back(request, session, lead, exc)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/task-suggestions/{suggestion_id}/dismiss", response_class=Response)
def dismiss(
    request: Request,
    suggestion_id: int,
    session: Annotated[Session, Depends(db.get_session)],
    lead: LeadUser,
):
    try:
        dismiss_task(session, lead, suggestion_id)
    except Exception as exc:  # noqa: BLE001
        return _back(request, session, lead, exc)
    return RedirectResponse("/inbox", status_code=303)
