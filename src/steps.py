"""The steps of a task, and where a task is among them.

Every task type has a default list of steps (CLAUDE.md, "Task types and stages"); a lead
can give one task its own list (`Task.steps`, `None` = the type's default). A step may be
a *review* step: the task waits there for a lead.

`Task.stage` holds the name of the step the task is on. It is the one stored position,
and the status has to agree with it, always:

    todo          on a step to work on (never a review step)
    in_progress   on a step to work on (never a review step)
    needs_review  on a review step
    done          past the last step

`current_index` enforces that when reading, so old or hand-entered rows can't show
"Needs review" while the strip says Drawing. Every move (`landing`, `work_index`) writes
a stage and status that agree. Pure functions over plain values: no database here.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from models import TASK_TYPES, Task

MAX_STEPS = 12
MAX_NAME_CHARS = 40

# type -> ((step name, is a review step), ...)
DEFAULT_STEPS: dict[str, tuple[tuple[str, bool], ...]] = {
    "Fabricate": (
        ("Drawing", False),
        ("Lead review", True),
        ("Process router", False),
        ("Shop", False),
        ("Inspect", False),
    ),
    "Print": (
        ("Check CAD", False),
        ("Lead review", True),
        ("Print", False),
        ("Fit check", False),
    ),
    "Design": (
        ("Requirements", False),
        ("Concepts", False),
        ("Lead review", True),
        ("CAD", False),
        ("Fit check", False),
    ),
    "Validate": (
        ("Test plan", False),
        ("Lead approval", True),
        ("Run", False),
        ("Log results", False),
    ),
    "Investigate": (
        ("Gather info", False),
        ("Findings", False),
        ("Lead review", True),
    ),
}
# A task whose type isn't one of the above still needs steps to move through.
FALLBACK_STEPS = (("Work", False), ("Lead review", True))

# What each type means, for the type picker.
TYPE_HINTS = {
    "Fabricate": "machined part",
    "Print": "3D printed part",
    "Design": "design work",
    "Validate": "test or check",
    "Investigate": "find out or decide something",
}

# Stage words from before these lists existed, and the step they now mean.
LEGACY_STAGES = {"ai pre-check": "drawing", "calculations": "test plan"}


@dataclass(frozen=True)
class Step:
    name: str
    review: bool = False


def clean_name(value: str | None) -> str:
    return " ".join((value or "").split())


def default_steps(task_type: str | None) -> list[Step]:
    spec = DEFAULT_STEPS.get(task_type or "", FALLBACK_STEPS)
    return [Step(name, review) for name, review in spec]


def custom_steps(task: Task) -> list[Step] | None:
    """The task's own steps, or None when it uses its type's (or the stored list is bad)."""
    stored = task.steps
    if not isinstance(stored, list) or not stored:
        return None
    found = []
    for item in stored:
        if not isinstance(item, dict):
            return None
        name = clean_name(item.get("name") if isinstance(item.get("name"), str) else "")
        if not name:
            return None
        found.append(Step(name, bool(item.get("review"))))
    return found


def steps_for(task: Task) -> list[Step]:
    return custom_steps(task) or default_steps(task.task_type)


def as_json(steps: Iterable[Step]) -> list[dict]:
    return [{"name": s.name, "review": s.review} for s in steps]


def match_stage(stage: str | None, steps: list[Step]) -> int | None:
    """The position of the step a stage word means, or None.

    Exact name first, then a name the word starts with ("Shop (machine)" is Shop),
    then an old stage word's replacement. Case and spacing don't matter.
    """
    word = clean_name(stage).casefold()
    if not word:
        return None
    names = [s.name.casefold() for s in steps]
    if word in names:
        return names.index(word)
    for i, name in enumerate(names):
        if word.startswith(name):
            return i
    legacy = LEGACY_STAGES.get(word)
    if legacy and legacy in names:
        return names.index(legacy)
    return None


def work_index(steps: list[Step], index: int) -> int:
    """The step to work on at or before `index`: the nearest one that isn't a review."""
    index = min(max(index, 0), len(steps) - 1)
    for i in range(index, -1, -1):
        if not steps[i].review:
            return i
    return next((i for i, s in enumerate(steps) if not s.review), 0)


def review_index(steps: list[Step], index: int) -> int | None:
    """The review step at `index` or the next one after it (else the last one)."""
    ahead = [i for i, s in enumerate(steps) if s.review and i >= index]
    if ahead:
        return ahead[0]
    behind = [i for i, s in enumerate(steps) if s.review]
    return behind[-1] if behind else None


def current_index(task: Task, steps: list[Step] | None = None) -> int:
    """Where the task is, agreeing with its status. `len(steps)` means finished."""
    steps = steps if steps is not None else steps_for(task)
    if task.status == "done":
        return len(steps)
    found = match_stage(task.stage, steps)
    index = found if found is not None else 0
    if task.status == "needs_review":
        review = review_index(steps, index)
        return review if review is not None else index
    return work_index(steps, index)


def fit_stage(task_type: str, status: str, stage: str | None) -> str:
    """The stage name to store for a task made from a file: the stage it came with,
    moved to the nearest step that agrees with its status (empty = the first step)."""
    probe = Task(code="", title="", subsystem="", task_type=task_type, status=status)
    probe.stage = stage or ""
    steps = steps_for(probe)
    here = current_index(probe, steps)
    return steps[min(here, len(steps) - 1)].name


def landing(steps: list[Step], index: int) -> tuple[str, str]:
    """(status, stage) for a task arriving at step `index`; past the end is done."""
    if index >= len(steps):
        return "done", steps[-1].name
    return ("needs_review" if steps[index].review else "in_progress"), steps[index].name


def strip(task: Task) -> list[dict]:
    """Each step as {"label", "state", "review"}: past, current or ahead."""
    steps = steps_for(task)
    here = current_index(task, steps)
    return [
        {
            "label": s.name,
            "review": s.review,
            "state": "current" if i == here else "past" if i < here else "ahead",
        }
        for i, s in enumerate(steps)
    ]


def current_step(task: Task) -> Step | None:
    """The step the task is on, or None once it is finished."""
    steps = steps_for(task)
    here = current_index(task, steps)
    return steps[here] if here < len(steps) else None


def next_step(task: Task) -> Step | None:
    """The step after the current one, or None at the end."""
    steps = steps_for(task)
    here = current_index(task, steps)
    return steps[here + 1] if here + 1 < len(steps) else None


def then_text(task: Task) -> str:
    """The "Then" of a plan (Plan my next steps): where the task goes after the step it is on.
    Read from the task's steps, with no model call."""
    steps = steps_for(task)
    here = current_index(task, steps)
    if here >= len(steps):
        return "Every step of this task is done."
    current = steps[here].name
    if here + 1 >= len(steps):
        return f"{current} is the last step. When it is done, the task is finished."
    following = steps[here + 1]
    if following.review:
        return (
            f"{following.name} comes after {current}: ask for review, and a lead checks "
            "the work before it moves on."
        )
    return f"{following.name} is the next step after {current}."


# --- changing a task's type ---


def retype(task: Task, new_type: str) -> tuple[str, str]:
    """(stage, status) after changing the type. Raises ValueError if the type is unknown.

    A task with its own steps keeps them and its place in them: only the label changes.
    Otherwise it moves to the new type's list. It keeps its current step when the new
    list has a step of that name; else it starts that list over, or, if it was waiting
    for review, waits at the new list's first review step.
    """
    if new_type not in TASK_TYPES:
        raise ValueError(new_type)
    if custom_steps(task):
        return task.stage, task.status
    old_steps = steps_for(task)
    here = current_index(task, old_steps)
    new_steps = default_steps(new_type)
    if task.status == "done":
        return new_steps[-1].name, "done"
    named = match_stage(old_steps[here].name, new_steps)
    if task.status == "needs_review":
        index = review_index(new_steps, named or 0)
        assert index is not None  # every default list has a review step
        return new_steps[index].name, "needs_review"
    return new_steps[work_index(new_steps, named or 0)].name, task.status


# --- editing a task's steps (the lead's editor) ---


@dataclass
class Row:
    """One line of the editor. `orig` is the step's position before editing (None if new)."""

    name: str
    review: bool = False
    orig: int | None = None


def rows_for(task: Task) -> list[Row]:
    return [Row(s.name, s.review, i) for i, s in enumerate(steps_for(task))]


def default_rows(task: Task) -> list[Row]:
    """The type's default steps as editor rows, keeping the current step's place."""
    old = steps_for(task)
    here = current_index(task, old)
    named = (
        match_stage(old[here].name, default_steps(task.task_type))
        if here < len(old)
        else None
    )
    rows = []
    for i, step in enumerate(default_steps(task.task_type)):
        rows.append(Row(step.name, step.review, here if i == named else None))
    return rows


def apply_op(rows: list[Row], op: str, task: Task) -> list[Row]:
    """Apply one editor button (add, reset, up:N, down:N, remove:N) to the rows."""
    rows = [Row(r.name, r.review, r.orig) for r in rows]
    kind, _, arg = op.partition(":")
    if kind == "add":
        if len(rows) < MAX_STEPS:
            rows.append(Row("", False, None))
        return rows
    if kind == "reset":
        return default_rows(task)
    if not arg.isdigit() or int(arg) >= len(rows):
        return rows
    i = int(arg)
    if kind == "remove":
        del rows[i]
    elif kind == "up" and i > 0:
        rows[i - 1], rows[i] = rows[i], rows[i - 1]
    elif kind == "down" and i < len(rows) - 1:
        rows[i + 1], rows[i] = rows[i], rows[i + 1]
    return rows


def validate_rows(rows: list[Row], task: Task) -> str | None:
    """Why these rows can't be saved as the task's steps (plain words), or None."""
    names = [clean_name(r.name) for r in rows]
    if len(rows) < 2:
        return "A task needs at least two steps."
    if len(rows) > MAX_STEPS:
        return f"A task can have at most {MAX_STEPS} steps."
    if any(not n for n in names):
        return "Every step needs a name. Fill in the empty one or remove it."
    if any(len(n) > MAX_NAME_CHARS for n in names):
        return f"Step names can be at most {MAX_NAME_CHARS} characters."
    if len({n.casefold() for n in names}) != len(names):
        return "Two steps have the same name. Give each step its own name."
    if rows[0].review:
        return "The first step can't be a lead review: there is nothing to review yet."
    if not any(r.review for r in rows):
        return "Mark at least one step as a lead review. Leads approve the work."
    if task.status in ("in_progress", "needs_review"):
        here = current_index(task)
        kept = next((r for r in rows if r.orig == here), None)
        current = steps_for(task)[here]
        if kept is None:
            return (
                f"“{current.name}” is the step this task is on, so it can't be removed. "
                "Finish or approve it first, or rename it instead."
            )
        if kept.review != current.review:
            return (
                f"“{current.name}” is the step this task is on, so its lead-review "
                "setting can't change yet. Finish or approve it first."
            )
    return None


def save_rows(task: Task, rows: list[Row]) -> list[Step]:
    """Write validated rows onto the task: its steps, and the stage that follows them.

    The step the task is on follows its row (a rename keeps the place). A list equal to
    the type's default is stored as "no custom steps". Status is not changed here; the
    validation guarantees it still agrees with the step.
    """
    here = current_index(task)
    steps = [Step(clean_name(r.name), r.review) for r in rows]
    moved = next((i for i, r in enumerate(rows) if r.orig == here), None)
    if task.status == "done":
        stage = steps[-1].name
    elif task.status == "needs_review":
        stage = steps[moved if moved is not None else review_index(steps, 0) or 0].name
    else:
        index = moved if moved is not None else min(here, len(steps) - 1)
        stage = steps[work_index(steps, index)].name
    task.steps = None if steps == default_steps(task.task_type) else as_json(steps)
    task.stage = stage
    return steps


def describe(steps: Iterable[Step]) -> str:
    return " → ".join(s.name for s in steps)
