from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    """Naive UTC, so SQLite (tests) and Postgres store and compare the same."""
    return datetime.now(UTC).replace(tzinfo=None)


# Display order matters: the task board lists subsystems in this order.
SUBSYSTEMS = (
    "Enclosure",
    "Seals/Waterproofing",
    "Electronics Mounts",
    "Thrusters",
    "Gripper",
    "Torpedo",
)
TASK_TYPES = ("Fabricate", "Design", "Validate")
STATUS_LABELS = {"todo": "To do", "in_progress": "In progress", "done": "Done"}
STATUSES = tuple(STATUS_LABELS)
DECISION_STATUS_LABELS = {
    "proposed": "Proposed",
    "confirmed": "Confirmed",
    "superseded": "Superseded",
}
DECISION_STATUSES = tuple(DECISION_STATUS_LABELS)


# Task text fields from the tracker, in display order, with their labels.
TASK_DETAIL_LABELS = {
    "description": "Description",
    "dependencies": "Dependencies",
    "exploratory_questions": "Exploratory questions",
    "skills": "Skills learned",
    "requirements": "Requirements",
    "people_to_ask": "People to ask",
    "documentation": "Documentation",
    "due_date": "Due date",
}


class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)  # e.g. "M04"
    title: str
    subsystem: str
    task_type: str  # Fabricate, Design, or Validate
    stage: str
    owner: str | None = None
    status: str = "todo"
    # Free-text fields from the team's tracker; the coach and task page show them.
    description: str | None = None
    dependencies: str | None = None
    exploratory_questions: str | None = None
    skills: str | None = None
    requirements: str | None = None
    people_to_ask: str | None = None
    documentation: str | None = None
    due_date: str | None = None  # text on purpose: the tracker has "Week 3", "TBD", ...


class CoachMessage(SQLModel, table=True):
    """One turn of a task's coach conversation."""

    __tablename__ = "coach_message"

    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    member: str
    role: str  # "user" or "assistant"
    content: str
    # JSON: {"sources": [{"title", "link", "modified"}], "notes": [str]}
    meta: str | None = None
    created_at: datetime = Field(default_factory=utc_now, index=True)


class LlmCall(SQLModel, table=True):
    """One model call, for the cost-benefit analysis. Never stores prompt text."""

    __tablename__ = "llm_call"

    id: int | None = Field(default=None, primary_key=True)
    task_id: int | None = Field(default=None, foreign_key="task.id", index=True)
    member: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int
    cost: float | None = None  # only when the response provides it
    status: str  # ok, budget_exceeded, rate_limited, timeout, error
    created_at: datetime = Field(default_factory=utc_now)


class Decision(SQLModel, table=True):
    """A team decision: members propose, leads confirm or supersede.

    A confirmed decision outranks anything the Drive says (see the coach prompt).
    """

    id: int | None = Field(default=None, primary_key=True)
    subsystem: str = Field(index=True)
    topic: str
    decision: str
    rationale: str
    # JSON lists: [{"option", "rejected_because"}] and [{"title", "link", "folder", "year"}]
    alternatives: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    sources: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    status: str = Field(default="proposed", index=True)  # proposed, confirmed, superseded
    superseded_by_id: int | None = Field(default=None, foreign_key="decision.id")
    proposed_by: str
    created_at: datetime = Field(default_factory=utc_now)
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    # True when the coach wrote the first draft (a member still reviewed and submitted it).
    drafted_by_coach: bool = False


class DecisionTask(SQLModel, table=True):
    """Links a decision to the tasks it relates to (optional, many to many)."""

    __tablename__ = "decision_task"

    decision_id: int = Field(foreign_key="decision.id", primary_key=True)
    task_id: int = Field(foreign_key="task.id", primary_key=True, index=True)
