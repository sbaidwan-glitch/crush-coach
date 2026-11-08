import re

import pytest
from fastapi.testclient import TestClient

import auth
import board
import main
from models import SUBSYSTEMS, Task


def add(session, code, title, subsystem, status="todo", owner=None):
    session.add(
        Task(
            code=code,
            title=title,
            subsystem=subsystem,
            task_type="Fabricate",
            stage="drawing",
            status=status,
            owner=owner,
        )
    )
    session.commit()


@pytest.fixture
def client():
    main.app.dependency_overrides[auth.require_user] = lambda: {
        "name": "Test Member",
        "email": "member@example.edu",
        "role": "member",
    }
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def headings(html: str) -> list[str]:
    found = re.findall(r"<h2[^>]*>\s*(.*?)\s*<span", html, flags=re.DOTALL)
    return [h.strip() for h in found]


def test_lists_tasks_grouped_by_subsystem_in_order(session, client):
    add(session, "T01", "Torpedo thing", "Torpedo")
    add(session, "M01", "Enclosure thing", "Enclosure")
    add(session, "G01", "Gripper thing", "Gripper", owner="Sam Example")

    response = client.get("/tasks")

    assert response.status_code == 200
    html = response.text
    assert headings(html) == list(SUBSYSTEMS)
    # Each task sits under its own subsystem heading, not another's.
    assert (
        html.index("Enclosure thing")
        < html.index("Gripper thing")
        < html.index("Torpedo thing")
    )
    assert "Owner: Sam Example" in html
    assert "Unclaimed" in html
    assert "Showing 3 of 3 tasks." in html


def test_empty_subsystems_still_appear(session, client):
    add(session, "M01", "Only one", "Enclosure")

    html = client.get("/tasks").text

    assert headings(html) == list(SUBSYSTEMS)
    assert html.count("No tasks.") == len(SUBSYSTEMS) - 1


def test_status_filter_shows_only_matching_tasks(session, client):
    add(session, "M01", "Not started", "Enclosure", status="todo")
    add(session, "M02", "Finished panel", "Enclosure", status="done")
    add(session, "G01", "In the shop", "Gripper", status="in_progress")

    html = client.get("/tasks", params={"status": "done"}).text

    assert "Finished panel" in html
    assert "Not started" not in html
    assert "In the shop" not in html
    assert "Showing 1 of 3 tasks with status “Done”." in html
    assert "Clear filter" in html
    assert '<option value="done" selected>' in html
    assert "No tasks with this status." in html


def test_unknown_status_shows_everything_with_a_notice(session, client):
    add(session, "M01", "Not started", "Enclosure", status="todo")

    response = client.get("/tasks", params={"status": "bogus"})

    assert response.status_code == 200
    assert "Unknown status &#39;bogus&#39;; showing all tasks." in response.text
    assert "Not started" in response.text


def test_task_text_is_escaped(session, client):
    add(session, "M01", "<script>alert(1)</script>", "Enclosure")

    html = client.get("/tasks").text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_page_is_accessible_and_phone_friendly(session, client):
    html = client.get("/tasks").text

    assert '<html lang="en">' in html
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in html
    assert "<title>Tasks – Crush Coach</title>" in html
    assert "<main>" in html
    assert html.count("<h1>") == 1
    assert '<label for="status">Status</label>' in html
    assert '<nav aria-label="Main">' in html


def test_page_says_plainly_when_the_database_fails(monkeypatch, client):
    def boom(session):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(board, "load_tasks", boom)

    response = client.get("/tasks")

    assert response.status_code == 503
    assert "Couldn&#39;t load tasks from the database right now." in response.text
    assert 'role="alert"' in response.text
    assert "secret" not in response.text
    # The rest of the page still works.
    assert '<nav aria-label="Main">' in response.text
    assert '<label for="status">Status</label>' in response.text
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200


def test_index_links_to_the_board(client):
    assert 'href="/tasks"' in client.get("/").text
