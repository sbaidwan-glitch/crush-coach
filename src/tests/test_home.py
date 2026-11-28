"""Home: your task with the stored Next step, tasks to pick up, progress, the render."""

from pathlib import Path

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
    main.app.dependency_overrides[auth.optional_user] = lambda: SAM
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


def test_signed_out_visitors_see_a_preview_with_blurred_tasks_they_cannot_claim(session):
    add(session, "E01", "Secret enclosure bracket", status="todo", owner="Alex Example")
    add(session, "E02", "Another real title", status="todo")

    with TestClient(main.app) as anonymous:
        response = anonymous.get("/")
    html = response.text

    assert response.status_code == 200
    assert 'href="/login">Sign in with Google</a>' in html
    assert '<nav class="sidebar__nav" aria-label="Main">' in html
    assert "You’re looking at a preview" in html
    # Real progress is shown; real titles, owners and links to tasks are not in the page.
    assert "Build complete" in html
    assert "Secret enclosure bracket" not in html
    assert "Another real title" not in html
    assert "Alex Example" not in html
    assert 'href="/tasks/E01"' not in html
    assert 'class="is-blurred"' in html
    # Claim is a disabled button, not a form that posts.
    assert "disabled" in html and 'action="/tasks/E01/status"' not in html


def test_your_task_is_the_members_task_in_progress_with_its_stage_strip(session, signed_in):
    add(session, "E01", "Map out electrical enclosure", status="in_progress", owner="Sam Example")
    add(session, "E02", "Someone else's", status="in_progress", owner="Alex Example")

    html = signed_in.get("/").text

    assert "Map out electrical enclosure" in html
    assert "Someone else&#39;s" not in html
    assert '<li class="is-current" aria-current="step">CAD</li>' in html
    assert 'action="/tasks/E01/status"' in html and 'value="advance"' in html
    assert ">Move to next stage</button>" in html
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


def test_the_next_step_block_is_a_link_into_the_coach_and_the_separate_link_is_gone(session, signed_in):
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

    block = html[html.index('<a class="active__next"') : html.index("</a>", html.index('<a class="active__next"'))]
    assert 'href="/tasks/E01#coach"' in block  # a real link, not a div with a handler
    assert "<b>Next step:</b> Open the sub-assembly." in block and "From the coach" in block
    assert '<span class="visually-hidden">, open the coach</span>' in block  # finishes its name
    assert '<span class="active__arrow" aria-hidden="true">↗</span>' in block  # decoration only
    assert "Ask the coach ↗" not in html and "active__ask" not in html
    assert "#coach-message" not in html
    # The buttons that remain.
    assert 'href="/tasks/E01"' in html and "Open task" in html and ">Move to next stage</button>" in html


def test_the_empty_next_step_is_the_same_link(session, signed_in):
    add(session, "E01", status="in_progress", owner="Sam Example")

    html = signed_in.get("/").text

    block = html[html.index('<a class="active__next"') : html.index("</a>", html.index('<a class="active__next"'))]
    assert 'href="/tasks/E01#coach"' in block
    assert "No next step yet. Ask the coach about this task and it will suggest one." in block
    assert ", open the coach</span>" in block and 'aria-hidden="true">↗' in block


def test_home_has_no_tagline_lines(session, signed_in):
    html = signed_in.get("/").text

    assert "One build. Clear next moves." not in html
    assert "One team. Every system moving." not in html
    assert "Build control" in html and "Crush" in html


def test_the_task_page_coach_section_is_the_target_of_that_link(session, signed_in):
    add(session, "E01", status="in_progress", owner="Sam Example")

    html = signed_in.get("/tasks/E01").text

    assert '<section class="panel" id="coach" aria-labelledby="coach-heading">' in html
    assert html.count('id="coach"') == 1 and 'id="coach-message"' in html


def test_arriving_with_coach_in_the_address_focuses_the_box_without_sending():
    js = (Path(__file__).resolve().parents[1] / "static" / "coach.js").read_text()
    focus = js[js.index("function focusCoachFromHash") : js.index('addEventListener("hashchange"')]
    assert 'addEventListener("load", focusCoachFromHash)' in focus  # the browser's jump clears focus first

    assert 'window.location.hash !== "#coach"' in focus
    assert 'byId("coach-message")' in focus and "field.focus()" in focus
    assert "requestSubmit" not in focus and "submit(" not in focus and "fetch(" not in focus


def test_a_member_with_no_task_is_told_to_pick_one(session, signed_in):
    add(session, "M01")

    html = signed_in.get("/").text

    assert "You haven’t started a task. Pick one from Available tasks below." in html


def test_a_task_on_a_review_stage_shows_when_nothing_else_is_in_progress(session, signed_in):
    add(session, "E01", "In review one", status="needs_review", owner="Sam Example", stage="Lead review")

    html = signed_in.get("/").text

    assert "In review one" in html
    assert ">Move to next stage</button>" in html  # nobody has to approve it first


def test_the_last_stage_says_finish_task_on_home(session, signed_in):
    add(session, "E01", status="in_progress", owner="Sam Example", stage="Fit check")

    html = signed_in.get("/").text

    assert ">Finish task</button>" in html and "Move to next stage" not in html


def test_the_review_tile_counts_unanswered_notifications_not_review_stages(session, signed_in):
    import review

    review_stage = add(session, "E01", status="needs_review", owner="Sam Example", stage="Lead review")
    asked = add(session, "E02", status="in_progress", owner="Sam Example")
    add(session, "E03", status="needs_review", owner="Alex Example", stage="Lead review")
    review.notify(session, asked, SAM, "")

    html = signed_in.get("/").text
    tile = html[html.index("Your tasks in review") - 120 : html.index("Your tasks in review") + 120]

    assert '<span class="tile__n">1</span>' in tile
    assert "Waiting for a lead to see" in tile and "Waiting for leads to check" not in html
    assert review_stage.status == "needs_review"  # sitting on a review stage adds nothing


def test_the_active_task_panel_shows_why_a_lead_sent_it_back(session, signed_in):
    import review

    task = add(session, "E01", status="in_progress", owner="Sam Example", stage="Lead review")
    task.status = "needs_review"
    session.add(task)
    session.commit()
    sent = review.notify(session, task, SAM, "Spacing ok?")
    lead = {"name": "Lee Lead", "email": "lee@example.edu", "role": "lead"}
    review.send_back(session, lead, sent.event.id, "Redo the hole spacing.")

    html = signed_in.get("/").text

    assert "Sent back:" in html and "Redo the hole spacing." in html


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
    assert "View all 6 →" in html
    assert "Owned" not in html.split('id="available"')[1]


def test_when_everything_has_an_owner_pick_up_says_so(session, signed_in):
    add(session, "M01", owner="Alex Example", status="in_progress")

    assert "Every task has an owner." in signed_in.get("/").text


def test_build_progress_comes_from_statuses(session, signed_in):
    add(session, "M01", status="done")
    add(session, "M02", status="in_progress", owner="A")
    add(session, "M03", subsystem="Thrusters")
    add(session, "M04", subsystem="Thrusters", status="needs_review", owner="B")

    html = signed_in.get("/").text

    assert "1 of 4 tasks done" in html
    assert 'aria-label="Build 25 percent complete"' in html
    # the gauge arc is the unfilled part of a circle of circumference 439.8: 75% of it
    assert "--off:329.9" in html
    assert 'aria-label="Enclosure: 1 of 2 done, 1 in progress"' in html
    assert 'aria-label="Thrusters: 0 of 2 done, 1 in progress"' in html
    assert 'href="/tasks?show=all#thrusters"' in html
    assert "width:50.0%" in html  # Enclosure: 1 of 2 done
    assert "width:0.0%" in html  # Thrusters: none done


def test_crush_render_has_a_tag_per_part_linking_to_its_tasks(session, signed_in):
    add(session, "M01", status="done")
    add(session, "M02", subsystem="Gripper", status="in_progress", owner="A")
    add(session, "M03", subsystem="Torpedo")

    html = signed_in.get("/").text

    assert '<img src="/static/img/crush.png" alt="CAD render of Crush">' in html
    assert 'href="/tasks?show=all#enclosure" style="left:54%;top:31%"' in html
    for slug in ("seals", "thrusters", "actuators"):
        assert f'class="render__tag" href="/tasks?show=all#{slug}"' in html
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
    assert "Vehicle overview" in response.text  # the rest of the page still renders
