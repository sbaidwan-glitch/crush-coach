"""The task status flow: who may move a task, what it records, and the buttons."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import auth
import main
import workflow
from models import Task, TaskEvent

SAM = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}
ALEX = {"name": "Alex Example", "email": "alex@example.edu", "role": "member"}
LEE = {"name": "Lee Lead", "email": "lee@example.edu", "role": "lead"}


def make_task(session, status="todo", owner=None, code="M04"):
    task = Task(
        code=code,
        title="Finish enclosure wall panels",
        subsystem="Enclosure",
        task_type="Fabricate",
        stage="shop",
        status=status,
        owner=owner,
    )
    session.add(task)
    session.commit()
    return task


def events(session, task):
    session.expire_all()
    return list(
        session.exec(
            select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
        ).all()
    )


# --- the rules, without a web request ---


def test_starting_an_unowned_task_claims_it_for_the_person(session):
    task = make_task(session)

    workflow.move(session, task, SAM, "start")

    assert (task.status, task.owner) == ("in_progress", "Sam Example")
    [event] = events(session, task)
    assert (event.actor, event.from_status, event.to_status) == (
        "Sam Example",
        "todo",
        "in_progress",
    )


def test_the_owner_matches_by_name_or_email_ignoring_case(session):
    task = make_task(session, owner="SAM EXAMPLE", status="in_progress")
    by_email = make_task(session, owner="Sam@Example.edu", status="in_progress", code="M05")

    workflow.move(session, task, SAM, "ready")
    workflow.move(session, by_email, SAM, "ready")

    assert task.status == by_email.status == "needs_review"


def test_a_member_cannot_move_someone_elses_task(session):
    task = make_task(session, status="in_progress", owner="Alex Example")

    for action in ("ready", "unclaim"):
        with pytest.raises(workflow.MoveError) as caught:
            workflow.move(session, task, SAM, action)
        assert caught.value.status == 403
    assert (task.status, task.owner) == ("in_progress", "Alex Example")
    assert events(session, task) == []


def test_a_member_cannot_start_a_task_someone_else_owns(session):
    task = make_task(session, owner="Alex Example")

    with pytest.raises(workflow.MoveError):
        workflow.move(session, task, SAM, "start")


def test_a_lead_can_move_any_task_without_taking_it_over(session):
    task = make_task(session, owner="Alex Example")

    workflow.move(session, task, LEE, "start")

    assert (task.status, task.owner) == ("in_progress", "Alex Example")
    assert events(session, task)[0].actor == "Lee Lead"


def test_ready_for_review_then_only_a_lead_can_approve(session):
    task = make_task(session, status="in_progress", owner="Sam Example")
    workflow.move(session, task, SAM, "ready")

    for action in ("approve", "send_back"):
        with pytest.raises(workflow.MoveError) as caught:
            workflow.move(session, task, SAM, action)  # not even the owner
        assert caught.value.status == 403
    assert task.status == "needs_review"

    workflow.move(session, task, LEE, "approve")

    assert task.status == "done"
    assert [(e.from_status, e.to_status) for e in events(session, task)] == [
        ("in_progress", "needs_review"),
        ("needs_review", "done"),
    ]


def test_send_back_returns_the_task_to_in_progress_and_keeps_the_note(session):
    task = make_task(session, status="needs_review", owner="Sam Example")

    workflow.move(session, task, LEE, "send_back", "  Check the hole spacing.  ")

    assert (task.status, task.owner) == ("in_progress", "Sam Example")
    assert events(session, task)[0].note == "Check the hole spacing."


def test_a_note_is_optional_and_only_kept_for_send_back(session):
    task = make_task(session, status="needs_review", owner="Sam Example")
    workflow.move(session, task, LEE, "send_back")
    assert events(session, task)[0].note is None

    workflow.move(session, task, SAM, "ready", "please look")
    assert events(session, task)[1].note is None


def test_a_note_is_cut_to_a_sane_length(session):
    task = make_task(session, status="needs_review", owner="Sam Example")

    workflow.move(session, task, LEE, "send_back", "x" * 5000)

    assert len(events(session, task)[0].note) == workflow.MAX_NOTE_CHARS


def test_unclaim_clears_the_owner_and_goes_back_to_to_do(session):
    task = make_task(session, status="in_progress", owner="Sam Example")

    workflow.move(session, task, SAM, "unclaim")

    assert (task.status, task.owner) == ("todo", None)


def test_unclaim_works_on_an_assigned_task_that_has_not_started(session):
    task = make_task(session, status="todo", owner="Sam Example")

    workflow.move(session, task, SAM, "unclaim")

    assert (task.status, task.owner) == ("todo", None)


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("todo", "ready"),
        ("todo", "approve"),
        ("in_progress", "start"),
        ("in_progress", "approve"),
        ("needs_review", "ready"),
        ("needs_review", "unclaim"),
        ("done", "unclaim"),
        ("done", "send_back"),
    ],
)
def test_a_move_the_flow_does_not_allow_is_refused_even_for_a_lead(session, status, action):
    task = make_task(session, status=status, owner="Sam Example")

    with pytest.raises(workflow.MoveError) as caught:
        workflow.move(session, task, LEE, action)

    assert caught.value.status == 409
    assert task.status == status
    assert events(session, task) == []


def test_an_unknown_action_is_a_400(session):
    task = make_task(session)

    with pytest.raises(workflow.MoveError) as caught:
        workflow.move(session, task, LEE, "delete")

    assert caught.value.status == 400


def test_available_actions_are_what_the_buttons_show(session):
    todo = make_task(session)
    mine = make_task(session, "in_progress", "Sam Example", code="M05")
    review = make_task(session, "needs_review", "Sam Example", code="M06")
    done = make_task(session, "done", "Sam Example", code="M07")

    assert workflow.available_actions(todo, SAM) == ["start"]
    assert workflow.available_actions(mine, SAM) == ["ready", "unclaim"]
    assert workflow.available_actions(mine, ALEX) == []
    assert workflow.available_actions(review, SAM) == []
    assert workflow.available_actions(review, LEE) == ["approve", "send_back"]
    assert workflow.available_actions(done, LEE) == []


# --- the page and the route ---


@pytest.fixture
def as_user():
    clients = []

    def make(user):
        main.app.dependency_overrides[auth.require_user] = lambda: user
        c = TestClient(main.app, follow_redirects=False)
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    main.app.dependency_overrides.clear()


def post(client, action, note="", next="", code="M04"):
    data = {"action": action, "note": note}
    if next:
        data["next"] = next
    return client.post(f"/tasks/{code}/status", data=data)


def test_start_button_claims_and_redirects_back_to_the_task(session, as_user):
    task = make_task(session)

    response = post(as_user(SAM), "start")

    assert response.status_code == 303
    assert response.headers["location"] == "/tasks/M04"
    session.expire_all()
    assert (task.status, task.owner) == ("in_progress", "Sam Example")


def test_the_route_returns_to_the_page_the_button_was_on(session, as_user):
    make_task(session)

    response = post(as_user(SAM), "start", next="/")

    assert response.headers["location"] == "/"


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example", "/\\evil"])
def test_the_route_never_redirects_to_another_site(session, as_user, target):
    make_task(session)

    response = post(as_user(SAM), "start", next=target)

    assert response.headers["location"] == "/tasks/M04"


def test_a_refused_move_says_why_on_the_task_page_and_changes_nothing(session, as_user):
    task = make_task(session, "in_progress", "Alex Example")

    response = post(as_user(SAM), "ready")

    assert response.status_code == 403
    assert "Only the person who owns this task, or a lead, can change it." in response.text
    assert "Finish enclosure wall panels" in response.text  # the page still works
    session.expire_all()
    assert task.status == "in_progress"
    assert events(session, task) == []


def test_a_member_cannot_approve_by_posting_the_form_themselves(session, as_user):
    task = make_task(session, "needs_review", "Sam Example")

    response = post(as_user(SAM), "approve")

    assert response.status_code == 403
    session.expire_all()
    assert task.status == "needs_review"


def test_a_stale_page_gets_a_409_with_a_plain_message(session, as_user):
    make_task(session, "needs_review", "Sam Example")

    response = post(as_user(SAM), "ready")

    assert response.status_code == 409
    assert "This task has already changed." in response.text


def test_unknown_task_is_a_404(session, as_user):
    assert post(as_user(SAM), "start", code="ZZ99").status_code == 404


def test_sign_in_is_required(session):
    make_task(session)
    main.app.dependency_overrides.clear()
    with TestClient(main.app, follow_redirects=False) as anonymous:
        response = anonymous.post("/tasks/M04/status", data={"action": "start"})

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_a_database_failure_is_a_plain_message_without_the_url(session, as_user, monkeypatch):
    make_task(session)

    def boom(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(workflow, "move", boom)

    response = post(as_user(SAM), "start")

    assert response.status_code == 503
    assert "Couldn&#39;t reach the database right now. Nothing was changed." in response.text
    assert "secret" not in response.text


def test_task_page_buttons_follow_who_is_looking(session, as_user):
    make_task(session)
    sam = as_user(SAM)
    assert "Start this task" in sam.get("/tasks/M04").text

    post(sam, "start")
    page = sam.get("/tasks/M04").text
    assert "Ready for review" in page and "Unclaim" in page
    assert "Start this task" not in page

    other = as_user(ALEX).get("/tasks/M04").text
    assert "Ready for review" not in other and "Unclaim" not in other
    assert "Only Sam Example or a lead can move this task." in other


def test_leads_see_approve_and_send_back_with_a_note_box_when_it_is_in_review(session, as_user):
    make_task(session, "needs_review", "Sam Example")

    lead = as_user(LEE).get("/tasks/M04").text
    assert 'value="approve"' in lead and 'value="send_back"' in lead
    assert 'name="note"' in lead
    assert "Note for the owner (optional)" in lead

    owner = as_user(SAM).get("/tasks/M04").text
    assert 'value="approve"' not in owner
    assert "Waiting for a lead to review it." in owner


def test_history_lists_each_move_newest_first_with_the_leads_note(session, as_user):
    make_task(session, "in_progress", "Sam Example")
    sam = as_user(SAM)
    post(sam, "ready")
    lead = as_user(LEE)
    post(lead, "send_back", note="Check the hole spacing.")

    page = lead.get("/tasks/M04").text

    assert "Lee Lead sent it back" in page and "“Check the hole spacing.”" in page
    assert "Sam Example sent it for review" in page
    assert page.index("Lee Lead sent it back") < page.index("Sam Example sent it for review")


def test_history_says_so_when_nothing_has_happened(session, as_user):
    make_task(session)

    assert "No changes yet." in as_user(SAM).get("/tasks/M04").text


def test_the_stage_strip_marks_the_current_stage(session, as_user):
    make_task(session)  # a Fabricate task in "shop"

    page = as_user(SAM).get("/tasks/M04").text

    assert '<li class="is-current" aria-current="step">Shop</li>' in page
    assert '<li class="is-past">Drawing</li>' in page
    assert "<li>Inspect</li>" in page
