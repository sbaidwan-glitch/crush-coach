from sqlmodel import Field, SQLModel

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


class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)  # e.g. "M04"
    title: str
    subsystem: str
    task_type: str  # Fabricate, Design, or Validate
    stage: str
    owner: str | None = None
    status: str = "todo"
