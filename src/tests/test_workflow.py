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


# Where a made-up Fabricate task sits for each status (its steps are Drawing, Lead
# review, Process router, Shop, Inspect).
STAGE_FOR = {"todo": "Drawing", "in_progress": "Drawing", "needs_review": "Lead review", "done": "Inspect"}


def make_task(session, status="todo", owner=None, code="M04", stage=None, task_type="Fabricate"):
    task = Task(
        code=code,
        title="Finish enclosure wall panels",
        subsystem="Enclosure",
        task_type=task_type,
        stage=stage or STAGE_FOR[status],
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

    workflow.move(session, task, SAM, "advance")
    workflow.move(session, by_email, SAM, "advance")

    assert task.status == by_email.status == "needs_review"


def test_a_member_cannot_move_someone_elses_task(session):
    task = make_task(session, status="in_progress", owner="Alex Example")

    for action in ("advance", "unclaim"):
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


def test_the_owner_moves_past_a_review_stage_themselves(session):
    task = make_task(session, status="in_progress", owner="Sam Example")
    workflow.move(session, task, SAM, "advance")
    assert (task.status, task.stage) == ("needs_review", "Lead review")

    event = workflow.move(session, task, SAM, "advance")  # nobody has to approve it

    assert (task.status, task.stage) == ("in_progress", "Process router")
    assert (event.kind, event.detail) == ("advance", "Lead review")
    assert [(e.from_status, e.to_status) for e in events(session, task)] == [
        ("in_progress", "needs_review"),
        ("needs_review", "in_progress"),
    ]


def test_there_is_no_approve_or_send_back_move_any_more(session):
    task = make_task(session, status="needs_review", owner="Sam Example")

    for action in ("approve", "send_back"):
        with pytest.raises(workflow.MoveError) as caught:
            workflow.move(session, task, LEE, action)  # not even a lead
        assert caught.value.status == 400
    assert task.status == "needs_review"
    assert events(session, task) == []


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
        ("todo", "advance"),
        ("in_progress", "start"),
        ("needs_review", "start"),
        ("needs_review", "unclaim"),
        ("done", "advance"),
        ("done", "unclaim"),
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
    assert workflow.available_actions(mine, SAM) == ["advance", "unclaim"]
    assert workflow.available_actions(mine, ALEX) == []
    assert workflow.available_actions(review, SAM) == ["advance"]  # a review stage is moved on from
    assert workflow.available_actions(review, ALEX) == []
    assert workflow.available_actions(review, LEE) == ["advance"]
    assert workflow.available_actions(done, LEE) == []


# --- the page and the route ---


@pytest.fixture
def as_user():
    clients = []

    def make(user):
        main.app.dependency_overrides[auth.require_user] = lambda: user
        main.app.dependency_overrides[auth.optional_user] = lambda: user
        c = TestClient(main.app, follow_redirects=False)
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    main.app.dependency_overrides.clear()


def post(client, action, next="", code="M04"):
    data = {"action": action}
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

    response = post(as_user(SAM), "advance")

    assert response.status_code == 403
    assert "Only the person who owns this task, or a lead, can change it." in response.text
    assert "Finish enclosure wall panels" in response.text  # the page still works
    session.expire_all()
    assert task.status == "in_progress"
    assert events(session, task) == []


def test_a_member_cannot_approve_by_posting_the_form_themselves(session, as_user):
    task = make_task(session, "needs_review", "Sam Example")

    response = post(as_user(SAM), "approve")

    assert response.status_code == 400  # there is no such move
    session.expire_all()
    assert task.status == "needs_review"


def test_a_stale_page_gets_a_409_with_a_plain_message(session, as_user):
    make_task(session, "todo", "Sam Example")

    response = post(as_user(SAM), "advance")  # not started yet

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
    assert ">Start</button>" in sam.get("/tasks/M04").text

    post(sam, "start")
    page = sam.get("/tasks/M04").text
    assert ">Move to next stage</button>" in page and "Unclaim" in page
    assert ">Start</button>" not in page
    assert "Ask for review" not in page and "Mark step done" not in page

    other = as_user(ALEX).get("/tasks/M04").text
    assert "Move to next stage" not in other and "Unclaim" not in other
    assert "Only Sam Example or a lead can move this task." in other


def test_the_task_page_shows_one_main_button_at_a_time(session, as_user):
    make_task(session)
    sam = as_user(SAM)

    def main_buttons():
        page = sam.get("/tasks/M04").text
        return [
            label
            for label in ("Start", "Move to next stage", "Finish task")
            if f">{label}</button>" in page
        ]

    assert main_buttons() == ["Start"]
    post(sam, "start")  # Drawing
    assert main_buttons() == ["Move to next stage"]
    post(sam, "advance")  # Lead review: still moves on, nobody waits for a lead
    assert main_buttons() == ["Move to next stage"]
    for _ in range(3):  # Process router, Shop, Inspect
        post(sam, "advance")
    assert main_buttons() == ["Finish task"]  # the last stage
    post(sam, "advance")
    assert main_buttons() == []


def test_a_task_on_a_review_stage_says_nobody_is_gating_it(session, as_user):
    make_task(session, "needs_review", "Sam Example")

    for user in (LEE, SAM):
        page = as_user(user).get("/tasks/M04").text
        assert ">Move to next stage</button>" in page
        assert "Waiting for a lead to check it." not in page
        assert 'value="approve"' not in page and 'value="send_back"' not in page
        assert "is a review stage" in page


def test_the_member_sees_why_a_lead_sent_the_task_back_until_they_move_it_on(session, as_user):
    import review

    task = make_task(session, "needs_review", "Sam Example")
    sam = as_user(SAM)
    done = review.notify(session, task, SAM, "Please check the holes")
    review.send_back(session, LEE, done.event.id, "Check the hole spacing.")

    page = sam.get("/tasks/M04").text
    assert "Sent back:" in page and "Check the hole spacing." in page

    post(sam, "advance")
    assert "Sent back:" not in sam.get("/tasks/M04").text


def test_history_lists_each_move_newest_first_with_the_leads_note(session, as_user):
    import review

    task = make_task(session, "in_progress", "Sam Example")
    sam = as_user(SAM)
    post(sam, "advance")
    done = review.notify(session, task, SAM, "Hole spacing ok?")
    lead = as_user(LEE)
    review.send_back(session, LEE, done.event.id, "Check the hole spacing.")

    page = lead.get("/tasks/M04").text

    assert "Lee Lead sent it back to “Drawing”" in page and "“Check the hole spacing.”" in page
    assert "Sam Example notified a lead about “Lead review”" in page and "Hole spacing ok?" in page
    assert "Sam Example finished “Drawing” and moved it to a review stage" in page
    assert page.index("Lee Lead sent it back") < page.index("Sam Example finished")
    assert "approved" not in page.lower()


def test_history_says_so_when_nothing_has_happened(session, as_user):
    make_task(session)

    assert "No changes yet." in as_user(SAM).get("/tasks/M04").text


def test_the_stage_strip_marks_the_current_stage(session, as_user):
    make_task(session, "in_progress", "Sam Example", stage="Shop")

    page = as_user(SAM).get("/tasks/M04").text

    assert '<li class="is-current" aria-current="step">Shop</li>' in page
    assert '<li class="is-past">Drawing</li>' in page
    assert "<li>Inspect</li>" in page
    assert "AI pre-check" not in page


# --- steps: Next step done, review steps, status and stage always agree ---


def test_next_step_done_moves_one_step_and_stays_in_progress(session):
    task = make_task(session, "in_progress", "Sam Example", task_type="Print", stage="Check CAD")
    task.stage = "Print"  # the third Print step: Check CAD, Lead review, Print, Fit check
    session.commit()

    event = workflow.move(session, task, SAM, "advance")

    assert (task.status, task.stage) == ("in_progress", "Fit check")
    assert (event.kind, event.detail) == ("advance", "Print")


def test_reaching_a_review_step_sets_needs_review_on_that_step(session):
    task = make_task(session, "in_progress", "Sam Example")  # Drawing

    workflow.move(session, task, SAM, "advance")

    assert (task.status, task.stage) == ("needs_review", "Lead review")


def test_moving_on_from_a_review_step_moves_past_it(session):
    task = make_task(session, "needs_review", "Sam Example")

    workflow.move(session, task, SAM, "advance")

    assert (task.status, task.stage) == ("in_progress", "Process router")


def test_a_lead_can_move_any_task_past_a_review_step_too(session):
    task = make_task(session, "needs_review", "Sam Example")

    event = workflow.move(session, task, LEE, "advance")

    assert (task.stage, event.actor) == ("Process router", "Lee Lead")


def test_finishing_the_last_step_completes_the_task(session):
    task = make_task(session, "in_progress", "Sam Example", stage="Inspect")

    event = workflow.move(session, task, SAM, "advance")

    assert (task.status, task.stage) == ("done", "Inspect")
    assert event.to_status == "done"


def test_moving_on_from_a_final_review_step_completes_the_task(session):
    task = make_task(
        session, "needs_review", "Sam Example", task_type="Investigate", stage="Lead review"
    )

    event = workflow.move(session, task, SAM, "advance")

    assert task.status == "done"
    assert event.to_status == "done"


def test_a_task_walks_through_every_step_of_its_type(session):
    task = make_task(session, "todo", task_type="Validate")
    seen = []

    workflow.move(session, task, SAM, "start")
    seen.append((task.status, task.stage))
    while task.status != "done":
        workflow.move(session, task, SAM, "advance")  # the owner, review stage or not
        seen.append((task.status, task.stage))

    assert seen == [
        ("in_progress", "Test plan"),
        ("needs_review", "Lead approval"),
        ("in_progress", "Run"),
        ("in_progress", "Log results"),
        ("done", "Log results"),
    ]


def test_status_and_stage_agree_after_every_move_through_a_review_stage(session):
    import steps

    task = make_task(session, "in_progress", "Sam Example")
    for _ in range(5):
        workflow.move(session, task, SAM, "advance")
        listed = steps.steps_for(task)
        here = steps.current_index(task, listed)
        if task.status == "done":
            assert here == len(listed)
        else:
            assert listed[here].name == task.stage
            assert (task.status == "needs_review") == listed[here].review


def test_a_stale_click_cannot_skip_a_step(session):
    task = make_task(session, "in_progress", "Sam Example", stage="Shop")
    workflow.move(session, task, SAM, "advance", expected_step="Shop")  # first click

    with pytest.raises(workflow.MoveError) as caught:
        workflow.move(session, task, SAM, "advance", expected_step="Shop")  # double click

    assert caught.value.status == 409
    assert task.stage == "Inspect"


def test_old_rows_that_disagree_are_read_as_agreeing(session):
    """Needs review while the stage says Drawing: the strip shows the review step."""
    import taskpage

    task = make_task(session, "needs_review", "Sam Example", stage="Drawing")

    current = [s["label"] for s in taskpage.stage_strip(task) if s["state"] == "current"]

    assert current == ["Lead review"]


# --- nobody approves, so nothing is "approved" ---


def test_history_for_a_member_moving_on_from_a_review_stage_never_says_approved(session):
    import taskpage

    task = make_task(session, "needs_review", "Lee Lead")  # a lead's own task too

    event = workflow.move(session, task, LEE, "advance")

    assert event.kind == "advance"
    assert taskpage.event_text(event) == "Lee Lead moved on from “Lead review”"
    assert "approv" not in taskpage.event_text(event).lower()


def test_old_approval_rows_stay_readable_in_the_history(session):
    import taskpage
    from models import TaskEvent

    old = [
        TaskEvent(task_id=1, actor="Lee Lead", to_status="in_progress", kind="approve", detail="Lead review"),
        TaskEvent(task_id=1, actor="Lee Lead", to_status="in_progress", kind="self_approve", detail="Lead review"),
    ]

    assert taskpage.event_text(old[0]) == "Lee Lead approved “Lead review”"
    assert taskpage.event_text(old[1]) == "Lee Lead self-approved “Lead review”"


# --- a lead changes the type ---


def test_changing_the_type_switches_to_that_types_steps(session, as_user):
    task = make_task(session, "in_progress", "Sam Example", stage="Shop")
    lead = as_user(LEE)

    response = lead.post("/tasks/M04/type", data={"task_type": "Print"})

    assert response.status_code == 303
    session.expire_all()
    # No "Shop" in the Print steps: the task starts that list over, still in progress.
    assert (task.task_type, task.status, task.stage) == ("Print", "in_progress", "Check CAD")
    page = lead.get("/tasks/M04").text
    assert "Lee Lead changed the type: Fabricate → Print" in page
    assert "<li>Print</li>" in page and "Process router" not in page


def test_a_task_waiting_for_review_keeps_waiting_when_the_type_changes(session):
    task = make_task(session, "needs_review", "Sam Example")

    workflow.change_type(session, task, LEE, "Validate")

    assert (task.status, task.stage) == ("needs_review", "Lead approval")


def test_a_done_task_stays_done_when_the_type_changes(session):
    task = make_task(session, "done", "Sam Example")

    workflow.change_type(session, task, LEE, "Investigate")

    assert (task.status, task.stage) == ("done", "Lead review")


def test_only_a_lead_can_change_the_type(session, as_user):
    task = make_task(session, "in_progress", "Sam Example")

    response = as_user(SAM).post("/tasks/M04/type", data={"task_type": "Print"})

    assert response.status_code == 403
    session.expire_all()
    assert task.task_type == "Fabricate"


def test_an_unknown_type_is_refused(session):
    task = make_task(session)

    with pytest.raises(workflow.MoveError) as caught:
        workflow.change_type(session, task, LEE, "Paint")

    assert caught.value.status == 400


def test_the_type_picker_is_only_for_leads(session, as_user):
    make_task(session)

    assert 'name="task_type"' in as_user(LEE).get("/tasks/M04").text
    assert 'name="task_type"' not in as_user(SAM).get("/tasks/M04").text


# --- a lead edits the steps ---


def steps_form(*rows, op="save"):
    """rows are (name, review, orig). The form posts them in parallel lists."""
    return {
        "op": op,
        "name": [r[0] for r in rows],
        "orig": ["" if r[2] is None else str(r[2]) for r in rows],
        "review": [str(i) for i, r in enumerate(rows) if r[1]],
    }


def test_a_lead_saves_a_tasks_own_steps_and_the_task_keeps_its_place(session, as_user):
    task = make_task(session, "in_progress", "Sam Example", stage="Process router")
    lead = as_user(LEE)

    response = lead.post(
        "/tasks/M04/steps",
        data=steps_form(
            ("Drawing", False, 0),
            ("Lead review", True, 1),
            ("Order stock", False, None),
            ("Router sheet", False, 2),  # renamed: the task is on this one
            ("Shop", False, 3),
        ),
    )

    assert response.status_code == 303
    session.expire_all()
    assert (task.status, task.stage) == ("in_progress", "Router sheet")
    assert [s["name"] for s in task.steps] == [
        "Drawing", "Lead review", "Order stock", "Router sheet", "Shop"
    ]
    page = lead.get("/tasks/M04").text
    assert '<li class="is-current" aria-current="step">Router sheet</li>' in page
    assert "Lee Lead edited the steps" in page and "Order stock" in page


def test_steps_equal_to_the_default_are_stored_as_no_custom_steps(session):
    task = make_task(session, "todo")
    rows = stepslib_rows(task)

    workflow.edit_steps(session, task, LEE, rows)

    assert task.steps is None


def stepslib_rows(task):
    import steps

    return steps.rows_for(task)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([("Only", False, 0)], "at least two steps"),
        ([("Drawing", False, 0), ("", True, 1)], "needs a name"),
        ([("Drawing", False, 0), ("drawing", True, 1)], "same name"),
        ([("Review", True, None), ("Drawing", False, 0)], "nothing to review yet"),
        ([("Drawing", False, 0), ("Shop", False, 1)], "at least one step as a lead review"),
        ([("x" * 41, False, 0), ("Review", True, 1)], "at most 40"),
    ],
)
def test_bad_step_lists_are_refused_with_the_reason(session, as_user, rows, message):
    task = make_task(session, "todo")

    response = as_user(LEE).post("/tasks/M04/steps", data=steps_form(*rows))

    assert response.status_code == 400
    assert message in response.text
    session.expire_all()
    assert task.steps is None


def test_the_step_a_task_is_on_cannot_be_removed_or_turned_into_a_review(session, as_user):
    task = make_task(session, "in_progress", "Sam Example", stage="Shop")
    lead = as_user(LEE)

    removed = lead.post(
        "/tasks/M04/steps",
        data=steps_form(("Drawing", False, 0), ("Lead review", True, 1), ("Inspect", False, 4)),
    )
    as_review = lead.post(
        "/tasks/M04/steps",
        data=steps_form(
            ("Drawing", False, 0), ("Lead review", True, 1), ("Shop", True, 3), ("Inspect", False, 4)
        ),
    )

    assert removed.status_code == as_review.status_code == 400
    assert "is the step this task is on" in removed.text
    session.expire_all()
    assert task.steps is None


def test_reordering_and_removing_rows_only_changes_the_form_not_the_task(session, as_user):
    task = make_task(session, "todo")
    lead = as_user(LEE)
    rows = steps_form(("Drawing", False, 0), ("Lead review", True, 1), ("Shop", False, 3))

    moved = lead.post("/tasks/M04/steps", data={**rows, "op": "down:0"})
    removed = lead.post("/tasks/M04/steps", data={**rows, "op": "remove:2"})
    added = lead.post("/tasks/M04/steps", data={**rows, "op": "add"})

    assert moved.status_code == removed.status_code == added.status_code == 200
    assert moved.text.index('value="Lead review"') < moved.text.index('value="Drawing"')
    assert 'value="Shop"' not in removed.text
    assert added.text.count('name="name"') == 4
    session.expire_all()
    assert task.steps is None
    assert events(session, task) == []


def test_the_editor_alone_comes_back_for_htmx(session, as_user):
    make_task(session, "todo")
    rows = steps_form(("Drawing", False, 0), ("Lead review", True, 1))

    response = as_user(LEE).post(
        "/tasks/M04/steps", data={**rows, "op": "add"}, headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    assert response.text.lstrip().startswith("<form id=\"steps-form\"")
    assert "<html" not in response.text


def test_saving_through_htmx_redirects_the_whole_page(session, as_user):
    make_task(session, "todo")
    rows = steps_form(("Drawing", False, 0), ("Lead review", True, 1), ("Ship", False, None))

    response = as_user(LEE).post("/tasks/M04/steps", data=rows, headers={"HX-Request": "true"})

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/tasks/M04"


def test_a_member_cannot_edit_steps_and_does_not_see_the_editor(session, as_user):
    task = make_task(session, "in_progress", "Sam Example")
    sam = as_user(SAM)

    response = sam.post(
        "/tasks/M04/steps", data=steps_form(("Drawing", False, 0), ("Review", True, 1))
    )

    assert response.status_code == 403
    assert 'id="steps-form"' not in sam.get("/tasks/M04").text
    session.expire_all()
    assert task.steps is None


def test_a_custom_review_step_is_a_stage_the_owner_moves_through(session):
    task = make_task(session, "in_progress", "Sam Example", stage="Drawing")
    workflow.edit_steps(
        session,
        task,
        LEE,
        [
            stepslib_row("Drawing", False, 0),
            stepslib_row("Safety check", True, None),
            stepslib_row("Lead review", True, 1),
            stepslib_row("Shop", False, None),
        ],
    )

    workflow.move(session, task, SAM, "advance")
    assert (task.status, task.stage) == ("needs_review", "Safety check")
    workflow.move(session, task, SAM, "advance")
    assert (task.status, task.stage) == ("needs_review", "Lead review")  # two reviews in a row
    workflow.move(session, task, SAM, "advance")
    assert (task.status, task.stage) == ("in_progress", "Shop")


def stepslib_row(name, review, orig):
    import steps

    return steps.Row(name, review, orig)
