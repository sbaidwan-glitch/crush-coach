"""Remove tasks (/admin/import): preview what goes with them, then confirm and log it."""

import html
import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import func, select

import auth
import db
import main
import task_removal
from models import (
    CoachMessage,
    Decision,
    DecisionTask,
    LlmCall,
    Task,
    TaskEvent,
    TaskRemoval,
)

LEAD = {"name": "Test Lead", "email": "lead@example.edu", "role": "lead"}
MEMBER = {"name": "Test Member", "email": "member@example.edu", "role": "member"}


@pytest.fixture
def lead():
    main.app.dependency_overrides[auth.require_user] = lambda: LEAD
    with TestClient(main.app, follow_redirects=False) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def member():
    main.app.dependency_overrides[auth.require_user] = lambda: MEMBER
    with TestClient(main.app, follow_redirects=False) as c:
        yield c
    main.app.dependency_overrides.clear()


def add_task(session, code, title="Example task") -> Task:
    task = Task(code=code, title=title, subsystem="Gripper", task_type="Design", stage="CAD")
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def add_history(session, task, messages=2, calls=3, decision_topic=None, events=0):
    for i in range(events):
        session.add(
            TaskEvent(task_id=task.id, actor="Someone", from_status=None, to_status="todo")
        )
    for i in range(messages):
        session.add(
            CoachMessage(
                task_id=task.id,
                member="Someone",
                role="user" if i % 2 == 0 else "assistant",
                content=f"message {i}",
            )
        )
    for _ in range(calls):
        session.add(
            LlmCall(
                task_id=task.id,
                member="Someone",
                model="m",
                latency_ms=10,
                cost=0.01,
                status="ok",
            )
        )
    if decision_topic:
        decision = Decision(
            subsystem="Gripper",
            topic=decision_topic,
            decision="d",
            rationale="r",
            proposed_by="Someone",
            status="confirmed",
        )
        session.add(decision)
        session.commit()
        session.add(DecisionTask(decision_id=decision.id, task_id=task.id))
    session.commit()


def count(session, model) -> int:
    session.expire_all()
    return session.exec(select(func.count()).select_from(model)).one()


def fingerprint(page: str) -> str:
    return html.unescape(re.search(r'name="fingerprint" value="([^"]*)"', page).group(1))


def confirm(client, codes, fingerprint, **extra):
    data = {"code": codes, "fingerprint": fingerprint, "understood": "yes", **extra}
    return client.post("/admin/import/remove/confirm", data=data)


# --- the section on the page ---


def test_the_section_lists_tasks_and_offers_both_previews(lead, session):
    add_task(session, "M01", "First task")
    add_task(session, "M02", "Second <b>task</b>")

    page = lead.get("/admin/import").text

    assert "Remove tasks" in page
    assert 'name="code" value="M01"' in page and 'name="code" value="M02"' in page
    assert "Second &lt;b&gt;task&lt;/b&gt;" in page
    assert "Preview removing the ticked tasks" in page
    assert "Preview removing all imported tasks (2)" in page


def test_the_section_says_when_there_is_nothing_to_remove(lead, session):
    page = lead.get("/admin/import").text

    assert "There are no tasks in the tracker." in page
    assert "Preview removing" not in page


@pytest.mark.parametrize(
    ("path", "kwargs"),
    [
        ("/admin/import/remove/preview", {"data": {"scope": "all"}}),
        ("/admin/import/remove/confirm", {"data": {"code": ["M01"], "understood": "yes"}}),
    ],
)
def test_members_cannot_remove_anything(member, session, path, kwargs):
    add_task(session, "M01")

    response = member.post(path, **kwargs)

    assert response.status_code == 403
    assert count(session, Task) == 1


# --- preview ---


def test_preview_shows_what_goes_with_the_tasks_and_removes_nothing(lead, session):
    one, two = add_task(session, "M01", "First"), add_task(session, "M02", "Second")
    add_history(session, one, messages=2, calls=3, decision_topic="Latch style", events=4)
    add_history(session, two, messages=1, calls=0)

    response = lead.post("/admin/import/remove/preview", data={"scope": "picked", "code": ["M01"]})

    page = response.text
    assert response.status_code == 200
    assert "Nothing has been removed yet" in page
    assert "Remove 1 task?" in page
    assert "deletes 2 coach messages, 4 history entries and 1 link to decisions" in " ".join(
        page.split()
    )
    assert "3 model calls stay for the cost analysis" in " ".join(page.split())
    assert "Latch style (confirmed)" in page
    assert "Second" not in page
    assert count(session, Task) == 2
    assert count(session, CoachMessage) == 3


def test_all_imported_tasks_picks_every_task(lead, session):
    for code in ("M01", "M02", "E01"):
        add_task(session, code)

    page = lead.post("/admin/import/remove/preview", data={"scope": "all"}).text

    assert "Remove 3 tasks?" in page
    assert page.count('name="code" value=') == 3


def test_preview_without_a_choice_is_a_plain_error_on_the_same_page(lead, session):
    add_task(session, "M01")

    response = lead.post("/admin/import/remove/preview", data={"scope": "picked"})

    assert response.status_code == 400
    assert "Tick at least one task" in response.text
    assert 'type="file"' in response.text  # the page is still usable


def test_preview_of_everything_with_no_tasks_says_so(lead, session):
    response = lead.post("/admin/import/remove/preview", data={"scope": "all"})

    assert response.status_code == 400
    assert "There are no tasks to remove." in response.text


# --- confirm ---


def test_confirm_removes_the_tasks_with_their_messages_and_links_and_keeps_model_calls(
    lead, session
):
    gone, kept = add_task(session, "M01", "Going"), add_task(session, "M02", "Staying")
    add_history(session, gone, messages=2, calls=3, decision_topic="Latch style", events=2)
    add_history(session, kept, messages=1, calls=1, events=1)
    gone_id = gone.id
    preview = lead.post("/admin/import/remove/preview", data={"code": ["M01"]}).text

    response = confirm(lead, ["M01"], fingerprint(preview))

    assert response.status_code == 200
    assert "Removed 1 task, 2 coach messages, 2 history entries and 1 decision link." in " ".join(
        response.text.split()
    )
    session.expire_all()
    assert [t.code for t in session.exec(select(Task)).all()] == ["M02"]
    assert count(session, CoachMessage) == 1
    assert count(session, TaskEvent) == 1  # only the other task's History is left
    assert count(session, DecisionTask) == 0
    assert count(session, Decision) == 1  # the decision itself stays
    calls = session.exec(select(LlmCall)).all()
    assert len(calls) == 4  # every model call is kept
    assert sorted(c.task_id is None for c in calls) == [False, True, True, True]
    assert gone_id not in {c.task_id for c in calls}


def test_confirm_logs_who_removed_what(lead, session):
    one = add_task(session, "M01", "First")
    add_task(session, "M02", "Second")
    add_history(session, one, messages=2, calls=3, decision_topic="Latch style", events=3)
    preview = lead.post("/admin/import/remove/preview", data={"scope": "all"}).text

    confirm(lead, ["M01", "M02"], fingerprint(preview))

    session.expire_all()
    log = {r.task_code: r for r in session.exec(select(TaskRemoval)).all()}
    assert set(log) == {"M01", "M02"}
    assert log["M01"].removed_by == "lead@example.edu"
    assert log["M01"].task_title == "First"
    assert (
        log["M01"].coach_messages_deleted,
        log["M01"].history_entries_deleted,
        log["M01"].decision_links_deleted,
        log["M01"].llm_calls_unlinked,
    ) == (2, 3, 1, 3)
    assert log["M01"].batch == log["M02"].batch  # one confirmation, one batch
    assert log["M01"].removed_at is not None


def test_the_removal_log_is_shown_on_the_page(lead, session):
    add_task(session, "M01", "First")
    preview = lead.post("/admin/import/remove/preview", data={"code": ["M01"]}).text
    confirm(lead, ["M01"], fingerprint(preview))

    page = lead.get("/admin/import").text

    assert "Recently removed" in page
    assert "lead@example.edu" in page


def test_confirm_needs_the_box_ticked(lead, session):
    add_task(session, "M01")
    preview = lead.post("/admin/import/remove/preview", data={"code": ["M01"]}).text

    response = lead.post(
        "/admin/import/remove/confirm", data={"code": ["M01"], "fingerprint": fingerprint(preview)}
    )

    assert response.status_code == 400
    assert "Nothing was removed. Tick the box to confirm." in response.text
    assert count(session, Task) == 1
    assert count(session, TaskRemoval) == 0


def test_confirm_stops_if_the_tasks_changed_since_the_preview(lead, session):
    task = add_task(session, "M01")
    preview = lead.post("/admin/import/remove/preview", data={"code": ["M01"]}).text
    add_history(session, task, messages=1, calls=0)  # a member asks the coach meanwhile

    response = confirm(lead, ["M01"], fingerprint(preview))

    assert response.status_code == 409
    assert "Nothing was removed: these tasks changed since the preview" in response.text
    assert "deletes 1 coach message" in response.text  # the new numbers are shown
    assert count(session, Task) == 1
    assert count(session, TaskRemoval) == 0


def test_confirm_for_tasks_that_are_already_gone_removes_nothing(lead, session):
    response = confirm(lead, ["M01"], "whatever")

    assert response.status_code == 409
    assert "no longer in the tracker" in response.text
    assert count(session, TaskRemoval) == 0


def test_a_forged_fingerprint_removes_nothing(lead, session):
    add_task(session, "M01")

    response = confirm(lead, ["M01"], "0" * 16)

    assert response.status_code == 409
    assert count(session, Task) == 1


class FailsAtTheEnd:
    """A session whose commit fails after the work was queued."""

    def __init__(self, real):
        self.real = real
        self.rolled_back = False

    def __getattr__(self, name):
        return getattr(self.real, name)

    def commit(self):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    def rollback(self):
        self.rolled_back = True
        self.real.rollback()


def test_a_database_failure_removes_nothing_and_says_so(lead, session):
    task = add_task(session, "M01")
    add_history(session, task, messages=2, calls=1, decision_topic="Latch style", events=2)
    preview = lead.post("/admin/import/remove/preview", data={"code": ["M01"]}).text
    failing = FailsAtTheEnd(session)
    main.app.dependency_overrides[db.get_session] = lambda: failing
    try:
        response = confirm(lead, ["M01"], fingerprint(preview))
    finally:
        main.app.dependency_overrides.pop(db.get_session, None)

    assert response.status_code == 503
    assert "Nothing was removed" in response.text
    assert "secret" not in response.text
    assert failing.rolled_back
    assert count(session, Task) == 1
    assert count(session, CoachMessage) == 2
    assert count(session, DecisionTask) == 1
    assert count(session, TaskEvent) == 2
    assert count(session, TaskRemoval) == 0


# --- the module itself ---


def test_preview_ignores_codes_that_do_not_exist(session):
    add_task(session, "M01")

    plan = task_removal.preview(session, ["M01", "NOPE", ""])

    assert [t.code for t in plan.tasks] == ["M01"]


def test_the_fingerprint_changes_with_the_counts(session):
    task = add_task(session, "M01")
    before = task_removal.preview(session, ["M01"]).fingerprint
    add_history(session, task, messages=0, calls=1)

    assert task_removal.preview(session, ["M01"]).fingerprint != before
