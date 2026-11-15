"""The lead review queue: Home panel, the Tasks link's count, and /tasks for leads."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import auth
import main
import shell
from models import Task, TaskEvent, utc_now

MEMBER = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}
LEAD = {"name": "Lee Lead", "email": "lee@example.edu", "role": "lead"}


@pytest.fixture
def as_user(monkeypatch):
    """Sign in as a user for both the page dependency and the shell's own lookup."""
    clients = []

    def make(user):
        monkeypatch.setattr(auth, "session_user", lambda request: user)
        main.app.dependency_overrides[auth.require_user] = lambda: user
        c = TestClient(main.app)
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    main.app.dependency_overrides.clear()


def add(session, code, status="needs_review", owner="Alex Example", waited_days=None, **kw):
    task = Task(
        code=code,
        title=kw.pop("title", f"Task {code}"),
        subsystem="Enclosure",
        task_type="Fabricate",
        stage="Lead review" if status == "needs_review" else "Drawing",
        status=status,
        owner=owner,
    )
    session.add(task)
    session.commit()
    if waited_days is not None:
        session.add(
            TaskEvent(
                task_id=task.id,
                actor=owner or "x",
                from_status="in_progress",
                to_status="needs_review",
                kind="advance",
                created_at=utc_now() - timedelta(days=waited_days),
            )
        )
        session.commit()
    return task


def test_home_shows_leads_a_panel_of_what_waits_longest_first(session, as_user):
    add(session, "M01", title="Newer one", waited_days=1)
    add(session, "M02", title="Older one", owner="Riley Sample", waited_days=4)
    add(session, "M03", status="in_progress", title="Not waiting")

    html = as_user(LEAD).get("/").text

    assert "Waiting for your review" in html
    assert "Older one" in html and "Newer one" in html and "Not waiting" not in html
    assert html.index("Older one") < html.index("Newer one")
    assert "Riley Sample · waiting 4 days" in html
    assert 'href="/tasks/M02"' in html and "Review" in html
    # It sits above Your task.
    assert html.index("Waiting for your review") < html.index("Your task")


def test_home_panel_says_when_nothing_is_waiting(session, as_user):
    html = as_user(LEAD).get("/").text

    assert "Waiting for your review" in html
    assert "Nothing is waiting for your review." in html


def test_members_get_no_review_panel_and_no_count(session, as_user):
    add(session, "M01", waited_days=1)

    html = as_user(MEMBER).get("/").text

    assert "Waiting for your review" not in html
    assert "waiting for review" not in html
    assert "sidebar__badge" not in html


def test_the_tasks_link_shows_leads_how_many_are_waiting_on_every_page(session, as_user):
    add(session, "M01")
    add(session, "M02")
    add(session, "M03", status="done")
    lead = as_user(LEAD)

    for path in ("/", "/tasks", "/tasks/M01", "/decisions"):
        html = lead.get(path).text
        assert '<span class="sidebar__badge" aria-hidden="true">2</span>' in html, path
        assert ", 2 waiting for review" in html, path


def test_no_count_when_nothing_is_waiting(session, as_user):
    add(session, "M01", status="in_progress")

    assert "sidebar__badge" not in as_user(LEAD).get("/tasks").text


def test_a_database_failure_hides_the_count_but_not_the_page(session, as_user, monkeypatch):
    add(session, "M01")
    lead = as_user(LEAD)

    class Broken:
        def __enter__(self):
            raise RuntimeError("postgresql://user:secret@host/db is down")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(shell, "Session", lambda engine: Broken())

    response = lead.get("/tasks")

    assert response.status_code == 200
    assert "sidebar__badge" not in response.text


def test_the_needs_review_filter_and_panel_render_for_leads(session, as_user):
    add(session, "M01", title="Waiting one", waited_days=2)
    add(session, "M02", status="in_progress", title="Busy one")
    lead = as_user(LEAD)

    html = lead.get("/tasks?show=review").text

    assert 'href="/tasks?show=review" aria-current="true">Needs review</a>' in html
    assert "Waiting one" in html.split("Recent decisions")[0]  # listed in its part of Crush
    assert "Busy one" not in html
    assert "Waiting for review" in html  # the panel
    assert "Owner: </span>Alex Example · waiting 2 days" in html
