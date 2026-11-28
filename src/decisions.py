"""Recorded answers (the `Decision` table, shown as the Record).

A decision is **recorded** (status `confirmed`) when a lead settles an answer; **replaced** (`superseded`) when a later answer to the
same question takes over. The database keeps its old names; only the words on the pages
changed (`models.DECISION_STATUS_LABELS`). Leads are the only people who record one, and
the routes that do it (`inbox.py`) check that on every POST.

A recorded answer outranks Drive search results (CLAUDE.md), so the coach looks them up
first (`search_confirmed`, called through `coach.py`). The helpers here take plain values
and a Session, not a Request, so tests and the coach can use them directly.
"""

import logging
import re
from datetime import datetime
from typing import Any

from sqlmodel import Session, col, select

import drive_write
import localtime
from auth import User
from models import (
    SUBSYSTEMS,
    Decision,
    DecisionTask,
    Task,
    utc_now,
)

log = logging.getLogger("crush_coach")
_SEARCH_TERM = re.compile(r"[a-z0-9][a-z0-9/-]+")
# Recorded answers come first: they are the ones people build from.
_STATUS_ORDER = {"confirmed": 0, "proposed": 1, "superseded": 2}


class RecordError(Exception):
    """A rule was broken (not a lead, nothing to record). The message is safe to show."""


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


# --- recording ----------------------------------------------------------------


def record_answer(
    session: Session,
    lead: User,
    *,
    subsystem: str,
    topic: str,
    answer: str,
    why: str = "",
    task: Task | None = None,
    sources: list[dict[str, Any]] | None = None,
    alternatives: list[dict[str, Any]] | None = None,
    replace_same_topic: bool = False,
) -> Decision:
    """Add a recorded answer (status `confirmed`) for a subsystem. Leads only.

    With `replace_same_topic`, a recorded answer in the same subsystem with the same
    topic is marked replaced and linked to this one: the newer answer to a question
    takes over. Adds to the session without committing, so the caller can make it part
    of a bigger change and commit once.
    """
    if lead["role"] != "lead":
        raise RecordError("Only a lead can record an answer.")
    topic, answer = topic.strip(), answer.strip()
    if subsystem not in SUBSYSTEMS:
        raise RecordError("That answer isn't about one of Crush's subsystems.")
    if not topic or not answer:
        raise RecordError("Type the answer to record.")
    now = utc_now()
    decision = Decision(
        subsystem=subsystem,
        topic=topic,
        decision=answer,
        rationale=why.strip(),
        alternatives=alternatives or [],
        sources=sources or [],
        status="confirmed",
        proposed_by=lead["name"],
        confirmed_by=lead["name"],
        confirmed_at=now,
        created_at=now,
    )
    session.add(decision)
    session.flush()  # the id, for the links below
    if task is not None:
        session.add(DecisionTask(decision_id=decision.id, task_id=task.id))
    if replace_same_topic:
        for old in session.exec(
            select(Decision).where(
                Decision.subsystem == subsystem, Decision.status == "confirmed"
            )
        ).all():
            if old.id != decision.id and old.topic.strip().casefold() == topic.casefold():
                old.status = "superseded"
                old.superseded_by_id = decision.id
                session.add(old)
    return decision


def write_to_drive(session: Session, decision_id: int) -> None:
    """Add a just-recorded answer to the Crush document (`drive_write.py`).

    Call it after the commit that recorded the answer, so the answer is saved whatever
    Google does. A failed write is kept on the entry (`DriveWrite`) for "Retry write to
    Drive"; this never raises, so recording never breaks on it.
    """
    try:
        decision = session.get(Decision, decision_id)
        if decision is not None and decision.status == "confirmed":
            drive_write.write_answer(session, decision)
    except Exception as exc:  # noqa: BLE001  (the answer is recorded; the write can be retried)
        session.rollback()
        log.error("Could not write a recorded answer to the Crush document (%s)", type(exc).__name__)


# --- the Record page ------------------------------------------------------------


def recorded_entries(
    session: Session, subsystems: tuple[str, ...], query: str = ""
) -> list[dict[str, Any]]:
    """Recorded answers for some subsystems, newest first, shaped for the Record page.

    Each has the answer, why, what it replaced, who recorded it and the tasks it came
    from. `query` keeps the ones whose topic, answer or why contain every word.
    """
    found = session.exec(
        select(Decision)
        .where(Decision.status == "confirmed", col(Decision.subsystem).in_(subsystems))
        .order_by(
            col(Decision.confirmed_at).desc(), col(Decision.created_at).desc(),
            col(Decision.id).desc(),
        )
    ).all()
    words = query.casefold().split()
    entries = []
    for d in found:
        haystack = f"{d.topic} {d.decision} {d.rationale}".casefold()
        if not all(word in haystack for word in words):
            continue
        replaced = session.exec(
            select(Decision).where(Decision.superseded_by_id == d.id)
        ).all()
        when = d.confirmed_at or d.created_at
        entries.append(
            {
                "kind": "answer",
                "id": d.id,
                "when": when,
                "subsystem": d.subsystem,
                "date": localtime.local_date(when),
                "topic": d.topic,
                "answer": d.decision,
                "why": d.rationale,
                "replaces": [r.decision for r in replaced],
                "recorded_by": d.confirmed_by or d.proposed_by,
                "tasks": task_codes_for(session, d.id),
                "sources": d.sources,
            }
        )
    return entries


def decision_location(session: Session, decision_id: int) -> tuple[str, int] | None:
    """(subsystem, id) of a decision, for the old /decisions/N links."""
    d = session.get(Decision, decision_id)
    return (d.subsystem, d.id) if d is not None else None
