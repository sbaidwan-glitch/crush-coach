"""What the Notebook's test files share: people, a scripted model and a few row makers.
Made-up people and tasks only. The `as_user` and `doc` fixtures are in conftest.py."""

import fake_openai as fake
from sqlmodel import select

import llm
import localtime
from models import Task, WorkNote, utc_now

MEMBER = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}
OTHER = {"name": "Riley Sample", "email": "riley@example.edu", "role": "member"}
LEAD = {"name": "Lee Lead", "email": "lee@example.edu", "role": "lead"}
HTMX = {"HX-Request": "true"}
TODAY = localtime.heading(localtime.today())


def use_model(monkeypatch, *script):
    model = fake.FakeClient(list(script))
    monkeypatch.setattr(llm, "get_client", lambda: model)
    return model


def add_task(session, code="M06", owner="Sam Example", status="in_progress", **kw):
    task = Task(
        code=code,
        title=kw.pop("title", "Weld the enclosure walls"),
        subsystem=kw.pop("subsystem", "Enclosure"),
        task_type="Fabricate",
        stage="Drawing",
        status=status,
        owner=owner,
        **kw,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def add_note(session, task, member=MEMBER, did="Cut the panels.", why="", when=None, **kw):
    note = WorkNote(
        member=member["email"], member_name=member["name"], task_id=task.id,
        task_code=task.code, subsystem=task.subsystem, did=did, why=why, original=did,
        posted_at=when or utc_now(), **kw,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def stored(session, model):
    session.expire_all()
    return list(session.exec(select(model)).all())
