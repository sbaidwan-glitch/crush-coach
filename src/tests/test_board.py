import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import auth
import board
import main
from models import Decision, Task, TaskEvent, TaskSuggestion, utc_now

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
    main.app.dependency_overrides[auth.optional_user] = lambda: user
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
        assert re.search(rf'<section class="panel panel--flush" data-tone="\w+" id="{slug}"', html)
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
    assert "You haven&#39;t claimed a task yet." in html and "?show=unclaimed" in html

    html = client.get("/tasks", params={"show": "review"}).text
    assert "No tasks match this filter." in html and 'href="/tasks?show=all"' in html


# --- Mine by default, and the marker on your own rows ---


def seed_mixed(session):
    add(session, "M01", "My own thing", "Enclosure", status="in_progress", owner="Test Member")
    add(session, "M02", "Their thing", "Enclosure", status="in_progress", owner="Someone Else")
    add(session, "M03", "Free thing", "Seals/Waterproofing")


def test_a_member_with_tasks_lands_on_mine_and_all_is_one_click_away(session, client):
    seed_mixed(session)

    html = client.get("/tasks").text

    assert '<a href="/tasks?show=mine" aria-current="true">Mine</a>' in html
    assert "My own thing" in html and "Their thing" not in html and "Free thing" not in html
    assert '<a href="/tasks?show=all">All</a>' in html
    everything = client.get("/tasks", params={"show": "all"}).text
    assert '<a href="/tasks?show=all" aria-current="true">All</a>' in everything
    assert all(t in everything for t in ("My own thing", "Their thing", "Free thing"))


def test_a_member_with_no_tasks_yet_lands_on_all_not_an_empty_page(session, client):
    add(session, "M02", "Their thing", "Enclosure", status="in_progress", owner="Someone Else")

    html = client.get("/tasks").text

    assert '<a href="/tasks?show=all" aria-current="true">All</a>' in html
    assert "Their thing" in html


def test_leads_and_signed_out_visitors_land_on_all(session):
    add(session, "M01", "Lead's own", "Enclosure", status="in_progress", owner="Test Lead")
    add(session, "M02", "Their thing", "Enclosure", status="in_progress", owner="Someone Else")

    with sign_in_as(LEAD) as lead:
        lead_html = lead.get("/tasks").text
    main.app.dependency_overrides.clear()
    with TestClient(main.app, follow_redirects=False) as visitor:
        visitor_html = visitor.get("/tasks").text

    assert '<a href="/tasks?show=all" aria-current="true">All</a>' in lead_html
    assert "Their thing" in lead_html
    assert '<a href="/tasks?show=all" aria-current="true">All</a>' in visitor_html
    assert ">Mine<" not in visitor_html


def test_own_tasks_are_marked_yours_on_all_tasks_and_others_are_not(session, client):
    seed_mixed(session)

    html = client.get("/tasks", params={"show": "all"}).text

    rows = re.findall(r'<div class="task-row[^"]*">.*?</div>', html, re.DOTALL)
    by_code = {re.search(r'class="code">(\w+)<', r).group(1): r for r in rows}
    assert "task-row--mine" in by_code["M01"] and ">Yours<" in by_code["M01"]
    for code in ("M02", "M03"):
        assert "task-row--mine" not in by_code[code] and "Yours" not in by_code[code]


def test_home_links_to_the_board_go_to_all_not_mine(session, client):
    seed_mixed(session)

    html = client.get("/").text

    assert 'href="/tasks?show=all">All tasks</a>' in html
    assert 'href="/tasks?show=all#enclosure"' in html


# --- a member's own suggestions ---


def suggest(session, who, title, status="open", task_code=None):
    session.add(
        TaskSuggestion(title=title, why="", suggested_by=who, status=status, task_code=task_code)
    )
    session.commit()


def test_your_suggestions_show_each_state_and_nobody_elses(session, client):
    add(session, "M09", "Drill jig", "Enclosure")
    suggest(session, "Test Member", "Waiting one")
    suggest(session, "Test Member", "Added one", status="added", task_code="M09")
    suggest(session, "Test Member", "Passed one", status="dismissed")
    suggest(session, "Someone Else", "Not mine to see")

    html = client.get("/tasks").text

    panel = html[html.index('id="your-suggestions"') :].split("</section>")[0]
    assert "Your suggestions" in panel
    for title in ("Waiting one", "Added one", "Passed one"):
        assert title in panel
    assert "Waiting for a lead" in panel and ">Added<" in panel and ">Not now<" in panel
    assert 'href="/tasks/M09"' in panel
    assert "Not mine to see" not in html


def test_your_suggestions_stay_when_a_filter_hides_every_task_row(session, client):
    suggest(session, "Test Member", "Still here")

    html = client.get("/tasks", params={"show": "review"}).text

    assert "Still here" in html


def test_no_your_suggestions_panel_before_you_suggest_anything(session, client):
    assert "Your suggestions" not in client.get("/tasks").text


def test_a_new_suggestion_shows_up_straight_away_as_waiting(session, client):
    client.post("/task-suggestions", data={"title": "Spare O-rings", "why": "We keep losing them"})

    html = client.get("/tasks").text

    assert "Spare O-rings" in html and "Waiting for a lead" in html


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


def test_the_board_has_no_recently_recorded_panel(session, client):
    session.add(
        Decision(
            topic="Topic 0", subsystem="Enclosure", decision="Answer 0", rationale="r",
            proposed_by="x", status="confirmed", confirmed_at=utc_now(),
        )
    )
    session.commit()

    html = client.get("/tasks").text

    assert "Recently recorded" not in html
    assert "Topic 0" not in html  # recorded answers live on the Record page


def test_the_board_has_no_review_queue_for_anyone(session):
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
        member_html = member.get("/tasks").text
    with sign_in_as(LEAD) as lead:
        lead_html = lead.get("/tasks").text
    main.app.dependency_overrides.clear()

    for html in (member_html, lead_html):
        assert "Waiting for review" not in html and "waiting 2 days" not in html
    assert "Confirm seal" in lead_html  # still listed with the other tasks


def test_signed_in_people_can_suggest_a_task_from_the_board(session, client):
    html = client.get("/tasks").text

    assert "Suggest a task" in html and 'action="/task-suggestions"' in html
    assert 'name="title"' in html and 'name="why"' in html


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


# --- signed-out preview ---


@pytest.fixture
def anonymous():
    main.app.dependency_overrides.clear()  # the real sign-in applies
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def test_signed_out_board_shows_real_counts_but_only_stand_in_rows(session, anonymous):
    add(session, "M01", "Cut the secret panel", "Enclosure", status="done", owner="Alex Example")
    add(session, "M02", "Drill the hidden lid", "Enclosure", status="todo")

    response = anonymous.get("/tasks")
    html = response.text

    assert response.status_code == 200
    assert "You’re looking at a preview" in html
    assert "1/2" in html  # the real "done" count
    assert "M01" in html and "M02" in html  # codes and status are fine to show
    for private in ("Cut the secret panel", "Drill the hidden lid", "Alex Example"):
        assert private not in html
    assert 'href="/tasks/M01"' not in html
    assert 'class="is-blurred"' in html
    assert ">Mine<" not in html  # no "Mine" without an account


def test_signed_out_board_does_not_read_decisions(session, anonymous):
    session.add(
        Decision(
            topic="Secret topic", subsystem="Enclosure", decision="d", rationale="r",
            proposed_by="x", status="confirmed",
        )
    )
    session.commit()

    html = anonymous.get("/tasks").text

    assert "Secret topic" not in html
    assert "Recently recorded" not in html
    assert "Suggest a task" not in html  # signed-out visitors can't send anything


def test_signed_out_visitors_cannot_claim_a_task(session, anonymous):
    add(session, "M02", "Drill the hidden lid", "Enclosure")

    response = anonymous.post("/tasks/M02/status", data={"action": "start"})

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    session.expire_all()
    assert session.exec(select(Task)).one().status == "todo"
