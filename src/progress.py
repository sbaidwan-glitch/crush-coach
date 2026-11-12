"""Build progress: how the tasks roll up into Crush's four parts.

Pure functions over `Task` rows, so the Home and Tasks pages (and the tests) share
one set of numbers. Everything comes from `Task.status`: done, started (in progress
or waiting for review) and not started.

Crush is shown in four parts, not the six subsystems the tracker uses: Electronics
Mounts sit with Enclosure, and Gripper and Torpedo are both Actuators (docs/design.md).
"""

from collections.abc import Iterable
from dataclasses import dataclass

from models import Task

# One entry per part of Crush, in display order. `colour` is the number of the
# --color-sub-N token. `tag` is where its label sits on the render, as percentages
# of the image (left, top): move a tag here if the render changes.
SUBSYSTEM_GROUPS = (
    {
        "slug": "enclosure",
        "label": "Enclosure",
        "subsystems": ("Enclosure", "Electronics Mounts"),
        "colour": 1,
        "tag": (54, 31),
    },
    {
        "slug": "seals",
        "label": "Seals",
        "subsystems": ("Seals/Waterproofing",),
        "colour": 2,
        "tag": (66, 50),
    },
    {
        "slug": "thrusters",
        "label": "Thrusters",
        "subsystems": ("Thrusters",),
        "colour": 3,
        "tag": (12, 38),
    },
    {
        "slug": "actuators",
        "label": "Actuators",
        "subsystems": ("Gripper", "Torpedo"),
        "colour": 4,
        "tag": (69, 66),
    },
)

STARTED = ("in_progress", "needs_review")


@dataclass
class GroupProgress:
    slug: str
    label: str
    colour: int
    tag: tuple[int, int]
    tasks: list[Task]
    total: int
    done: int
    in_progress: int  # started: in progress, or waiting for review

    @property
    def open(self) -> int:
        return self.total - self.done

    @property
    def done_pct(self) -> float:
        return 100 * self.done / self.total if self.total else 0

    @property
    def started_pct(self) -> float:
        return 100 * (self.done + self.in_progress) / self.total if self.total else 0

    @property
    def tag_state(self) -> str:
        """The status mark on Crush's tag: all done, started, or not started."""
        if self.total and self.done == self.total:
            return "done"
        if self.done or self.in_progress:
            return "in_progress"
        return "todo"

    @property
    def aria_label(self) -> str:
        text = f"{self.label}: {self.done} of {self.total} done"
        if self.in_progress:
            text += f", {self.in_progress} in progress"
        return text


def group_progress(tasks: Iterable[Task]) -> list[GroupProgress]:
    """Every part of Crush, in order, with its tasks and counts (empty parts included)."""
    tasks = list(tasks)
    groups = []
    for spec in SUBSYSTEM_GROUPS:
        mine = [t for t in tasks if t.subsystem in spec["subsystems"]]
        groups.append(
            GroupProgress(
                slug=spec["slug"],
                label=spec["label"],
                colour=spec["colour"],
                tag=spec["tag"],
                tasks=mine,
                total=len(mine),
                done=sum(1 for t in mine if t.status == "done"),
                in_progress=sum(1 for t in mine if t.status in STARTED),
            )
        )
    return groups


def overall(groups: Iterable[GroupProgress]) -> dict[str, int]:
    """Total tasks, done, and the rounded percentage done."""
    groups = list(groups)
    total = sum(g.total for g in groups)
    done = sum(g.done for g in groups)
    return {"total": total, "done": done, "percent": round(100 * done / total) if total else 0}


def is_unclaimed(task: Task) -> bool:
    return not task.owner and task.status == "todo"


def owned_by(task: Task, name: str, email: str) -> bool:
    """Whether this person owns the task. Imported owners are names, claims store the
    name too; an email is accepted so a hand-entered owner still matches."""
    owner = (task.owner or "").strip().casefold()
    return bool(owner) and owner in (name.strip().casefold(), email.strip().casefold())


def counts(tasks: Iterable[Task]) -> dict[str, int]:
    """The numbers in the counts box on /tasks."""
    tasks = list(tasks)
    return {
        "total": len(tasks),
        "done": sum(1 for t in tasks if t.status == "done"),
        "in_progress": sum(1 for t in tasks if t.status == "in_progress"),
        "review": sum(1 for t in tasks if t.status == "needs_review"),
        "unclaimed": sum(1 for t in tasks if is_unclaimed(t)),
    }
