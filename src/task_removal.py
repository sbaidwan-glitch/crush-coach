"""Removing tasks (lead-only, from /admin/import): what goes with them, and the log.

A removal is previewed first (`preview`), then confirmed (`remove`). The preview
counts what would go with each task:

* its coach messages and its History (status moves) are deleted,
* its links to decisions are deleted (the decisions themselves stay),
* its model calls (`llm_call`) stay for the cost analysis but lose their task.

`remove` does all of it in one transaction, and writes one `TaskRemoval` row per
task saying who removed it and what went with it.

The preview carries a fingerprint of those counts. If a coach message arrives
between the preview and the confirm, the fingerprint no longer matches and the lead
sees the new numbers before anything is deleted.
"""

import hashlib
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlmodel import Session, col, delete, select, update

from models import (
    CoachMessage,
    Decision,
    DecisionTask,
    LlmCall,
    Task,
    TaskEvent,
    TaskRemoval,
)

RECENT_REMOVALS = 10


@dataclass
class TaskImpact:
    """One task to remove and what goes with it."""

    code: str
    title: str
    coach_messages: int = 0
    history_entries: int = 0
    decision_links: int = 0
    llm_calls: int = 0
    # (topic, status) of each linked decision. The decisions are not deleted.
    decisions: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class RemovalPreview:
    tasks: list[TaskImpact]
    fingerprint: str

    @property
    def coach_messages(self) -> int:
        return sum(t.coach_messages for t in self.tasks)

    @property
    def history_entries(self) -> int:
        return sum(t.history_entries for t in self.tasks)

    @property
    def decision_links(self) -> int:
        return sum(t.decision_links for t in self.tasks)

    @property
    def llm_calls(self) -> int:
        return sum(t.llm_calls for t in self.tasks)


class FingerprintMismatch(Exception):
    """What would be removed is not what the lead was shown."""

    def __init__(self, preview: RemovalPreview):
        super().__init__("the counts changed since the preview")
        self.preview = preview


def _fingerprint(tasks: Iterable[TaskImpact]) -> str:
    text = "|".join(
        f"{t.code}:{t.coach_messages}:{t.history_entries}:{t.decision_links}:{t.llm_calls}"
        for t in sorted(tasks, key=lambda t: t.code)
    )
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def all_tasks(session: Session) -> list[Task]:
    """Every task in the tracker, in code order (tasks only ever come from imports)."""
    return list(session.exec(select(Task).order_by(Task.code)).all())


def _counts(session: Session, column, ids: list[int]) -> dict[int, int]:
    rows = session.exec(
        select(column, func.count()).where(col(column).in_(ids)).group_by(column)
    ).all()
    return {task_id: count for task_id, count in rows}


def preview(session: Session, codes: Sequence[str]) -> RemovalPreview:
    """What removing these task codes would delete and keep. Changes nothing.

    Codes that are no longer in the tracker are ignored.
    """
    wanted = {code for code in codes if code}
    found = (
        session.exec(
            select(Task).where(col(Task.code).in_(wanted)).order_by(Task.code)
        ).all()
        if wanted
        else []
    )
    ids = [t.id for t in found if t.id is not None]
    messages = _counts(session, CoachMessage.task_id, ids) if ids else {}
    events = _counts(session, TaskEvent.task_id, ids) if ids else {}
    links = _counts(session, DecisionTask.task_id, ids) if ids else {}
    calls = _counts(session, LlmCall.task_id, ids) if ids else {}

    decisions: dict[int, list[tuple[str, str]]] = {}
    if ids:
        for task_id, topic, status in session.exec(
            select(DecisionTask.task_id, Decision.topic, Decision.status)
            .join(Decision, col(Decision.id) == col(DecisionTask.decision_id))
            .where(col(DecisionTask.task_id).in_(ids))
            .order_by(Decision.topic)
        ).all():
            decisions.setdefault(task_id, []).append((topic, status))

    impacts = [
        TaskImpact(
            code=t.code,
            title=t.title,
            coach_messages=messages.get(t.id, 0),
            history_entries=events.get(t.id, 0),
            decision_links=links.get(t.id, 0),
            llm_calls=calls.get(t.id, 0),
            decisions=decisions.get(t.id, []),
        )
        for t in found
    ]
    return RemovalPreview(impacts, _fingerprint(impacts))


def remove(
    session: Session, codes: Sequence[str], removed_by: str, fingerprint: str
) -> RemovalPreview:
    """Remove the tasks and log it, all or nothing.

    Raises FingerprintMismatch (and changes nothing) if the counts are no longer the
    ones the lead confirmed. The caller rolls back on any other error.
    """
    current = preview(session, codes)
    if current.fingerprint != fingerprint:
        raise FingerprintMismatch(current)
    if not current.tasks:
        return current

    codes_now = [t.code for t in current.tasks]
    ids = list(session.exec(select(Task.id).where(col(Task.code).in_(codes_now))).all())
    batch = uuid.uuid4().hex
    for impact in current.tasks:
        session.add(
            TaskRemoval(
                batch=batch,
                removed_by=removed_by,
                task_code=impact.code,
                task_title=impact.title,
                coach_messages_deleted=impact.coach_messages,
                history_entries_deleted=impact.history_entries,
                decision_links_deleted=impact.decision_links,
                llm_calls_unlinked=impact.llm_calls,
            )
        )
    # Children first. Model calls are kept, just unlinked.
    session.exec(delete(CoachMessage).where(col(CoachMessage.task_id).in_(ids)))
    session.exec(delete(TaskEvent).where(col(TaskEvent.task_id).in_(ids)))
    session.exec(delete(DecisionTask).where(col(DecisionTask.task_id).in_(ids)))
    session.exec(
        update(LlmCall).where(col(LlmCall.task_id).in_(ids)).values(task_id=None)
    )
    session.exec(delete(Task).where(col(Task.id).in_(ids)))
    session.commit()
    return current


def recent(session: Session, limit: int = RECENT_REMOVALS) -> list[TaskRemoval]:
    return list(
        session.exec(
            select(TaskRemoval)
            .order_by(col(TaskRemoval.removed_at).desc(), col(TaskRemoval.id).desc())
            .limit(limit)
        ).all()
    )
