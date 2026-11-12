"""Home: your task with the stored Next step, tasks to pick up, progress, the render."""

import pytest
from fastapi.testclient import TestClient

import auth
import board
import home
import llm
import main
from models import CoachMessage, Task, utc_now

SAM = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}


@pytest.fixture(autouse=True)
def model_is_off_limits(monkeypatch):
    """Home must never call the model: any attempt fails the test."""

    def refuse(*args, **kwargs):
        raise AssertionError("Home called the model")

    monkeypatch.setattr(llm, "get_client", refuse)
    monkeypatch.setattr(llm, "CoachStream", refuse)


@pytest.fixture
def client():
    main.app.dependency_overrides[auth.require_user] = lambda: SAM
    # Home reads the session itself, so give it a real signed-in cookie too.
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def signed_in(monkeypatch, client):
    monkeypatch.setattr(auth, "session_user", lambda request: SAM)
    monkeypatch.setattr(home.auth, "session_user", lambda request: SAM)
    return client


def add(session, code, title="Task", subsystem="Enclosure", status="todo", owner=None, **kw):
    task = Task(
        code=code,
        title=title,
        subsystem=subsystem,
        task_type=kw.pop("task_type", "Design"),
        stage=kw.pop("stage", "CAD"),
        status=status,
        owner=owner,
    )
    session.add(task)
    session.commit()
    return task


def test_signed_out_visitors_get_a_sign_in_panel_in_the_same_shell(session):
    with TestClient(main.app) as anonymous:
        response = anonymous.get("/")

    assert response.status_code == 200
    assert 'href="/login">Sign in with Google</a>' in response.text
    assert '<nav class="sidebar" aria-label="Main">' in response.text
    assert "Your task" not in response.text


def test_your_task_is_the_members_task_in_progress_with_its_stage_strip(session, signed_in):
    add(session, "E01", "Map out electrical enclosure", status="in_progress", owner="Sam Example")
    add(session, "E02", "Someone else's", status="in_progress", owner="Alex Example")

    html = signed_in.get("/").text

    assert "Map out electrical enclosure" in html
    assert "Someone else&#39;s" not in html
    assert '<li class="is-current" aria-current="step">CAD</li>' in html
    assert 'action="/tasks/E01/status"' in html and 'value="ready"' in html
    assert 'href="/tasks/E01"' in html


def test_next_step_is_the_stored_line_from_the_latest_coach_answer(session, signed_in):
    task = add(session, "E01", status="in_progress", owner="Sam Example")
    session.add(
        CoachMessage(
            task_id=task.id,
            member="Coach",
            role="assistant",
            content="Here is what I found.\n\n**Next step:** open the sub-assembly.",
            created_at=utc_now(),
        )
    )
    session.commit()

    html = signed_in.get("/").text

    assert "Open the sub-assembly." in html
    assert "From the coach" in html


def test_without_a_stored_next_step_home_says_so_and_does_not_ask_the_model(session, signed_in):
    add(session, "E01", status="in_progress", owner="Sam Example")

    html = signed_in.get("/").text  # (the autouse fixture fails if the model is touched)

    assert "No next step yet." in html


def test_a_member_with_no_task_is_told_to_pick_one(session, signed_in):
    add(session, "M01")

    html = signed_in.get("/").text

    assert "You haven’t started a task. Pick one on the right." in html


def test_a_task_waiting_for_review_shows_when_nothing_else_is_in_progress(session, signed_in):
    add(session, "E01", "In review one", status="needs_review", owner="Sam Example")

    html = signed_in.get("/").text

    assert "In review one" in html
    assert 'value="ready"' not in html  # already sent


def test_other_started_tasks_are_mentioned(session, signed_in):
    add(session, "E01", status="in_progress", owner="Sam Example")
    add(session, "E02", status="in_progress", owner="Sam Example")

    assert "You have 1 other task in progress." in signed_in.get("/").text


def test_pick_up_lists_at_most_four_unclaimed_tasks_with_a_claim_button(session, signed_in):
    for i in range(6):
        add(session, f"M0{i}", f"Free {i}")
    add(session, "M09", "Owned", owner="Alex Example")

    html = signed_in.get("/").text

    assert html.count('value="start"') == 4
    assert "6 open" in html
    assert "Owned" not in html.split("Build progress")[0].split("Pick up a task")[1]


def test_when_everything_has_an_owner_pick_up_says_so(session, signed_in):
    add(session, "M01", owner="Alex Example", status="in_progress")

    assert "Every task has an owner." in signed_in.get("/").text


def test_build_progress_comes_from_statuses(session, signed_in):
    add(session, "M01", status="done")
    add(session, "M02", status="in_progress", owner="A")
    add(session, "M03", subsystem="Thrusters")
    add(session, "M04", subsystem="Thrusters", status="needs_review", owner="B")

    html = signed_in.get("/").text

    assert "<b>1</b> / <b>4</b>" in html  # tasks done
    assert "<b>25</b>" in html  # percent
    assert 'aria-label="Enclosure: 1 of 2 done, 1 in progress"' in html
    assert 'aria-label="Thrusters: 0 of 2 done, 1 in progress"' in html
    assert 'href="/tasks#thrusters"' in html
    assert "1 open task<" in html  # singular


def test_crush_render_has_a_tag_per_part_linking_to_its_tasks(session, signed_in):
    add(session, "M01", status="done")
    add(session, "M02", subsystem="Gripper", status="in_progress", owner="A")
    add(session, "M03", subsystem="Torpedo")

    html = signed_in.get("/").text

    assert '<img src="/static/img/crush.png" alt="CAD render of Crush">' in html
    assert 'href="/tasks#enclosure" style="left:54%;top:31%"' in html
    for slug in ("seals", "thrusters", "actuators"):
        assert f'class="render__tag" href="/tasks#{slug}"' in html
    assert "Actuators 0/2" in html and "Enclosure 1/1" in html
    # a mark and words in each tag: all done / started / not started
    assert "Done: " in html and "Started: " in html and "Not started: " in html


def test_the_page_says_plainly_when_the_database_fails(monkeypatch, signed_in):
    def boom(session):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(board, "load_tasks", boom)

    response = signed_in.get("/")

    assert response.status_code == 503
    assert "Couldn&#39;t load tasks from the database right now." in response.text
    assert "secret" not in response.text
    assert "Build progress" in response.text  # the rest of the page still renders
