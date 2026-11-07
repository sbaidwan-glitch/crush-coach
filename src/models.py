from sqlmodel import Field, SQLModel


class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)  # e.g. "M04"
    title: str
    subsystem: str
    task_type: str  # Fabricate, Design, or Validate
    stage: str
    owner: str | None = None
    status: str = "todo"
