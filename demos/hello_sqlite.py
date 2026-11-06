"""Creates a small SQLite database with SQLModel, then reads it back.

    python demos/hello_sqlite.py

It writes demos/hello.db (gitignored, and recreated fresh every run). Then:

  - click hello.db in the Explorer to open it in the SQLite Viewer, or
  - query it from the terminal:  sqlite3 demos/hello.db "select * from idea;"
"""

from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select

DB = Path(__file__).with_name("hello.db")
DB.unlink(missing_ok=True)  # start fresh every run


class Idea(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    votes: int = 0


engine = create_engine(f"sqlite:///{DB}")
SQLModel.metadata.create_all(engine)

with Session(engine) as session:
    session.add(Idea(title="A to-do app, but for plants", votes=3))
    session.add(Idea(title="Dining hall wait-time tracker", votes=7))
    session.add(Idea(title="Lost-and-found board", votes=1))
    session.commit()

    for idea in session.exec(select(Idea).order_by(Idea.votes.desc())):
        print(f"  {idea.votes} votes  {idea.title}")

print(f"\n✅ Wrote and read back {DB}")
