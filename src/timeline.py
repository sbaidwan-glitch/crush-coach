"""The Timeline (`/timeline`): who picked up what, and when (docs/timeline.md).

One panel per part of Crush (the same tone as its progress bar), one row per task, and a
bar from the day the task was picked up to the day it was finished. Every date comes from
`TaskEvent`; there are no date fields on a task. Read-only, no model calls. Signed-out
visitors get the sign-in page (task titles and who did what are team content).

The date rules are one pure function, `span_for`, so they are tested without a database:

- Not started (status `todo`, which includes a task that was unclaimed): no bar.
- Start = the day of the task's latest "start" event, so a task that was unclaimed and
  picked up again starts again. With no start event (it was in progress before the tracker
  kept events) it starts at its first event.
- Done: ends the day of its latest "done" event (or its last event, if none says done).
- On a review stage: ends the day the task moved onto it.
- Otherwise open: runs up to today.
- A task that has started but has no events at all has no dates to draw from. It gets no
  bar and is named at the foot of its panel instead of being given an invented date.
"""

import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated, NamedTuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, col, select

import auth
import db
import localtime
import profiles
import progress
import review
import shell
from auth import MaybeUser
from models import STATUS_LABELS, Task, TaskEvent

log = logging.getLogger("crush_coach")
templates = shell.templates()
router = APIRouter()

WEEK = timedelta(days=7)
MIN_WEEKS = 8  # the chart is never narrower than this, so a young project isn't stretched
MAX_COLUMNS = 10  # past this, a column is two weeks, three... so the labels never collide


class Moment(NamedTuple):
    """One TaskEvent as the date rules read it: what kind of move, and the local day."""

    kind: str | None
    from_status: str | None
    to_status: str
    day: date


class Span(NamedTuple):
    """A bar: first day, last day, and how it is drawn (`done`, `open` or `review`)."""

    start: date
    end: date
    state: str


def moments(events: Iterable[TaskEvent]) -> list[Moment]:
    """TaskEvents in the order they happened, as days in the team's time zone. Telling a
    lead about a task, and a lead seeing it, aren't moves: they never draw a bar."""
    ordered = sorted(
        (e for e in events if e.kind not in review.NOT_MOVES),
        key=lambda e: (e.created_at, e.id or 0),
    )
    return [Moment(e.kind, e.from_status, e.to_status, localtime.local_date(e.created_at))
            for e in ordered]


def _is_start(m: Moment) -> bool:
    """A claim/start. Older rows have no kind: a move from todo to in progress is one."""
    return m.kind == "start" or (
        m.kind is None and m.from_status == "todo" and m.to_status == "in_progress"
    )


def span_for(status: str, events: Sequence[Moment], today: date) -> Span | None:
    """The bar for one task, or None when there is nothing to draw (see the module notes).

    `events` are in the order they happened. `status` is the task's status now, which
    decides how the bar ends; the events only supply the days.
    """
    if status == "todo" or not events:
        return None
    starts = [m for m in events if _is_start(m)]
    start = starts[-1].day if starts else events[0].day
    if status == "done":
        finished = [m for m in events if m.to_status == "done"]
        end = (finished[-1] if finished else events[-1]).day
        state = "done"
    elif status == "needs_review":
        asked = [m for m in events if m.to_status == "needs_review"]
        end = asked[-1].day if asked else today
        state = "review"
    else:
        end = today
        state = "open"
    # Odd data (a start recorded after the finish) must not draw a backwards bar.
    return Span(min(start, end), max(start, end), state)


@dataclass(frozen=True)
class Axis:
    """The chart's days: equal columns of whole weeks, starting on a Monday. A grid line
    and a date label sit at the start of each column."""

    start: date
    column_days: int
    columns: int

    @property
    def days(self) -> int:
        return self.column_days * self.columns

    @property
    def column_pct(self) -> float:
        return round(100 / self.columns, 2)

    def pct(self, day: date) -> float:
        return round(100 * (day - self.start).days / self.days, 2)

    def width_pct(self, first: date, last: date) -> float:
        """A bar's width; a one-day task still gets one day's width."""
        return round(100 * max(1, (last - first).days) / self.days, 2)

    def ticks(self) -> list[tuple[float, str]]:
        return [
            (
                round(100 * i / self.columns, 2),
                day_label(self.start + timedelta(days=i * self.column_days)),
            )
            for i in range(self.columns)
        ]


def day_label(day: date) -> str:
    return f"{localtime.MONTHS[day.month - 1]} {day.day}"


def axis_for(spans: Iterable[Span], today: date) -> Axis:
    """From a week before the earliest start to a week after the latest end (today counts
    as an end, so the Today line is always on the chart). It begins on a Monday and a
    column is one week (two, three... once there would be more than MAX_COLUMNS), so every
    label is a Monday."""
    spans = list(spans)
    first = min([s.start for s in spans], default=today)
    last = max([s.end for s in spans], default=today)
    last = max(last, today)
    start = first - WEEK
    start -= timedelta(days=start.weekday())
    weeks = max(MIN_WEEKS, math.ceil(((last + WEEK) - start).days / 7))
    step = math.ceil(weeks / MAX_COLUMNS)
    return Axis(start, 7 * step, math.ceil(weeks / step))


# --- the page ---


@dataclass
class Row:
    task: Task
    span: Span
    left: float
    width: float
    tip: float  # where the owner's chips sit: the bar's end
    chips: list[profiles.Chip]
    name: str  # "Riley", "Riley +1"
    label: str  # the bar's text for a screen reader
    state: str


@dataclass
class Panel:
    group: progress.GroupProgress
    rows: list[Row]
    not_started: list[str]  # task codes with no bar yet
    undated: list[str]  # started, but no event says when


def _range_text(span: Span) -> str:
    end = "today" if span.state == "open" else day_label(span.end)
    return f"{day_label(span.start)} – {end}"


def build(
    tasks: list[Task],
    events: dict[int, list[Moment]],
    people: profiles.Profiles,
    today: date,
) -> tuple[Axis, list[Panel], float]:
    """The panels and axis for these tasks. Returns (axis, panels, today's % across)."""
    spans = {t.id: span_for(t.status, events.get(t.id, []), today) for t in tasks}
    axis = axis_for([s for s in spans.values() if s], today)
    panels = []
    for group in progress.group_progress(tasks):
        if not group.total:
            continue
        panel = Panel(group, [], [], [])
        for task in group.tasks:
            span = spans[task.id]
            if span is None:
                (panel.not_started if task.status == "todo" else panel.undated).append(task.code)
                continue
            chips = people.chips(task.owner)
            name = profiles.chip_label(chips)
            label = f"{task.title}: " + (f"{name} · " if name else "") + _range_text(span)
            panel.rows.append(
                Row(
                    task=task,
                    span=span,
                    left=axis.pct(span.start),
                    width=axis.width_pct(span.start, span.end),
                    tip=axis.pct(span.end),
                    chips=chips,
                    name=name,
                    label=label,
                    state=span.state,
                )
            )
        panels.append(panel)
    return axis, panels, axis.pct(today)


def load_events(session: Session, tasks: list[Task]) -> dict[int, list[Moment]]:
    """Every task's moves, grouped by task, in one query."""
    ids = [t.id for t in tasks if t.id is not None]
    rows = session.exec(
        select(TaskEvent).where(col(TaskEvent.task_id).in_(ids))
    ).all() if ids else []
    grouped: dict[int, list[TaskEvent]] = {}
    for event in rows:
        grouped.setdefault(event.task_id, []).append(event)
    return {task_id: moments(found) for task_id, found in grouped.items()}


@router.get("/timeline", response_class=HTMLResponse)
def timeline(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: MaybeUser,
):
    if user is None:
        return auth.locked_page(request, "the timeline")
    error = None
    context: dict = {"user": user, "status_labels": STATUS_LABELS, "panels": []}
    try:
        tasks = list(session.exec(select(Task).order_by(Task.code)).all())
        events = load_events(session, tasks)
        people = profiles.load(session)
        today = localtime.today()
        axis, panels, today_pct = build(tasks, events, people, today)
        context.update(
            panels=panels,
            axis=axis,
            ticks=axis.ticks(),
            column_pct=axis.column_pct,
            today_pct=today_pct,
            has_bars=any(p.rows for p in panels),
            total_tasks=len(tasks),
        )
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        session.rollback()
        # The error type only: the message can contain the connection URL.
        log.error("Could not load the timeline (%s)", type(exc).__name__)
        error = "Couldn't load the timeline from the database right now. Try again in a moment."
    context["error"] = error
    return templates.TemplateResponse(
        request, "timeline.html", context, status_code=503 if error else 200
    )
