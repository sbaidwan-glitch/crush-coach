import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import auth
import board
import main
from models import Decision, Task, TaskEvent, utc_now

MEMBER = {"name": "Test Member", "email": "member@example.edu", "role": "member"}
LEAD = {"name": "Test Lead", "email": "lead@example.edu", "role": "lead"}


def add(session, code, title, subsystem, status="todo", owner=None):
    task = Task(
        code=code,
        title=title,
        subsystem=subsystem,
        task_type="Fabricate",
        stage="drawing",
        status=status,
        owner=owner,
    )
    session.add(task)
    session.commit()
    return task


def sign_in_as(user):
    main.app.dependency_overrides[auth.require_user] = lambda: user
    return TestClient(main.app)


@pytest.fixture
def client():
    with sign_in_as(MEMBER) as c:
        yield c
    main.app.dependency_overrides.clear()


def test_lists_tasks_in_a_panel_per_part_of_crush_in_order(session, client):
    add(session, "T01", "Torpedo thing", "Torpedo")
    add(session, "M01", "Enclosure thing", "Enclosure")
    add(session, "G01", "Gripper thing", "Gripper", owner="Sam Example")
    add(session, "E01", "Tray thing", "Electronics Mounts")
    add(session, "M10", "Seal thing", "Seals/Waterproofing")
    add(session, "M20", "Thruster thing", "Thrusters")

    response = client.get("/tasks")

    assert response.status_code == 200
    html = response.text
    # Four parts: Electronics Mounts sit with Enclosure, Gripper and Torpedo are Actuators.
    for slug in ("enclosure", "seals", "thrusters", "actuators"):
        assert f'<section class="panel panel--flush" id="{slug}"' in html
    section = {
        slug: html[html.index(f'id="{slug}"') :]
        for slug in ("enclosure", "seals", "thrusters", "actuators")
    }
    assert "Tray thing" in section["enclosure"].split("</section>")[0]
    assert "Gripper thing" in section["actuators"].split("</section>")[0]
    assert "Torpedo thing" in section["actuators"].split("</section>")[0]
    assert "Sam Example" in html and "Unclaimed" in html


def test_each_panel_has_a_ruler_with_one_tick_per_task_and_the_words(session, client):
    add(session, "M01", "One", "Enclosure", status="done")
    add(session, "M02", "Two", "Enclosure", status="in_progress")
    add(session, "M03", "Three", "Electronics Mounts", status="needs_review")
    add(session, "M04", "Four", "Enclosure")

    html = client.get("/tasks").text

    assert "1 of 4 done" in html
    assert 'aria-label="Enclosure: 1 of 4 done, 2 in progress"' in html
    assert "--n:4;--done:25.00%;--started:75.00%" in html  # review counts as started


def test_counts_box_comes_from_statuses(session, client):
    add(session, "M01", "a", "Enclosure", status="done")
    add(session, "M02", "b", "Enclosure", status="in_progress", owner="X")
    add(session, "M03", "c", "Enclosure", status="needs_review", owner="Y")
    add(session, "M04", "d", "Seals/Waterproofing")
    add(session, "M05", "e", "Thrusters", owner="Z")  # owned but not started: not unclaimed

    html = client.get("/tasks").text

    counts = re.search(r'<div class="counts".*?</div>\s*</div>', html, re.DOTALL).group(0)
    assert re.findall(r"<b[^>]*>([^<]+)</b>", counts) == ["1/5", "1", "1", "1"]


def test_statuses_show_their_own_mark_and_words(session, client):
    add(session, "M01", "a", "Enclosure", status="todo")
    add(session, "M02", "b", "Enclosure", status="in_progress")
    add(session, "M03", "c", "Enclosure", status="needs_review")
    add(session, "M04", "d", "Enclosure", status="done")

    html = client.get("/tasks").text

    for mark, words in (
        ("todo", "To do"),
        ("in-progress", "In progress"),
        ("review", "Needs review"),
        ("done", "Done"),
    ):
        assert f'<span class="status status--{mark}">{words}</span>' in html


def test_filters_show_only_matching_rows_but_rulers_still_count_everything(session, client):
    add(session, "M01", "Mine one", "Enclosure", status="in_progress", owner="Test Member")
    add(session, "M02", "Theirs", "Enclosure", status="in_progress", owner="Someone Else")
    add(session, "M03", "Free one", "Seals/Waterproofing")
    add(session, "M04", "In review", "Thrusters", status="needs_review", owner="Q")

    mine = client.get("/tasks", params={"show": "mine"}).text
    assert "Mine one" in mine and "Theirs" not in mine and "Free one" not in mine
    assert 'aria-label="Enclosure: 0 of 2 done, 2 in progress"' in mine
    assert '<a href="/tasks?show=mine" aria-current="true">Mine</a>' in mine

    free = client.get("/tasks", params={"show": "unclaimed"}).text
    assert "Free one" in free and "Mine one" not in free
    assert 'id="seals"' in free and 'id="enclosure"' not in free  # empty panels hide

    review = client.get("/tasks", params={"show": "review"}).text
    assert "In review" in review and "Free one" not in review


def test_a_filter_with_no_matches_says_so(session, client):
    add(session, "M01", "Someone's", "Enclosure", owner="Someone Else", status="in_progress")

    html = client.get("/tasks", params={"show": "mine"}).text

    assert "No tasks match this filter." in html


def test_empty_board_says_so_and_how_it_gets_filled(session, client):
    response = client.get("/tasks")

    assert response.status_code == 200
    assert "No tasks yet." in response.text
    assert 'aria-label="Filter tasks"' in response.text  # filters still there


def test_unknown_filter_shows_everything_with_a_notice(session, client):
    add(session, "M01", "Not started", "Enclosure")

    response = client.get("/tasks", params={"show": "bogus"})

    assert response.status_code == 200
    assert "Unknown filter &#39;bogus&#39;; showing all tasks." in response.text
    assert "Not started" in response.text


def test_recent_decisions_show_three_newest_and_mark_proposed_ones(session, client):
    for i in range(4):
        session.add(
            Decision(
                subsystem="Enclosure",
                topic=f"Topic {i}",
                decision="d",
                rationale="r",
                status="proposed" if i == 3 else "confirmed",
                proposed_by="x",
                created_at=utc_now() + timedelta(minutes=i),
            )
        )
    session.commit()

    html = client.get("/tasks").text

    assert "Topic 3" in html and "Topic 2" in html and "Topic 1" in html
    assert "Topic 0" not in html
    assert "Proposed · needs a lead" in html


def test_waiting_for_review_is_for_leads_only(session):
    task = add(session, "M03", "Confirm seal", "Seals/Waterproofing", "needs_review", "Member B")
    session.add(
        TaskEvent(
            task_id=task.id,
            actor="Member B",
            from_status="in_progress",
            to_status="needs_review",
            created_at=utc_now() - timedelta(days=2),
        )
    )
    session.commit()

    with sign_in_as(MEMBER) as member:
        assert "Waiting for review" not in member.get("/tasks").text
    with sign_in_as(LEAD) as lead:
        html = lead.get("/tasks").text
    main.app.dependency_overrides.clear()
    assert "Waiting for review" in html
    assert "Member B · 2 days" in html
    assert 'href="/tasks/M03"' in html


def test_task_text_is_escaped(session, client):
    add(session, "M01", "<script>alert(1)</script>", "Enclosure")

    html = client.get("/tasks").text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_page_is_accessible_and_phone_friendly(session, client):
    html = client.get("/tasks").text

    assert '<html lang="en">' in html
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in html
    assert "<title>All tasks – Crush Coach</title>" in html
    assert '<main class="main" id="main">' in html
    assert html.count("<h1 ") == 1
    assert '<nav class="sidebar" aria-label="Main">' in html
    assert '<a class="sidebar__link" href="/tasks" aria-current="page">' in html
    assert 'role="group" aria-label="Task counts"' in html


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
    assert '<nav class="sidebar" aria-label="Main">' in response.text
    assert client.get("/health").status_code == 200


def test_decisions_failing_only_hides_that_panel(session, monkeypatch, client):
    add(session, "M01", "Still here", "Enclosure")

    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("db is down")

    monkeypatch.setattr(board, "Decision", Boom())

    response = client.get("/tasks")

    assert response.status_code == 200
    assert "Still here" in response.text
    assert "Couldn&#39;t load decisions right now." in response.text
