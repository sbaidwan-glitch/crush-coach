"""The step lists and where a task is in them (steps.py), plus the migration's stage fix."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text

import db
import import_tasks
import steps
from models import TASK_TYPES, Task


def task(task_type="Fabricate", status="in_progress", stage="Drawing", custom=None):
    return Task(
        code="M01",
        title="t",
        subsystem="Enclosure",
        task_type=task_type,
        stage=stage,
        status=status,
        steps=custom,
    )


def names(task_type):
    return [s.name for s in steps.default_steps(task_type)]


def test_each_type_has_exactly_the_agreed_steps():
    assert names("Fabricate") == ["Drawing", "Lead review", "Process router", "Shop", "Inspect"]
    assert names("Print") == ["Check CAD", "Lead review", "Print", "Fit check"]
    assert names("Design") == ["Requirements", "Concepts", "Lead review", "CAD", "Fit check"]
    assert names("Validate") == ["Test plan", "Lead approval", "Run", "Log results"]
    assert names("Investigate") == ["Gather info", "Findings", "Lead review"]
    assert set(TASK_TYPES) == {"Fabricate", "Print", "Design", "Validate", "Investigate"}


def test_no_default_list_has_an_ai_pre_check_step():
    for task_type in TASK_TYPES:
        assert "AI pre-check" not in names(task_type)


@pytest.mark.parametrize("task_type", TASK_TYPES)
def test_every_default_list_has_a_review_step_and_starts_with_work(task_type):
    listed = steps.default_steps(task_type)
    assert not listed[0].review
    assert any(s.review for s in listed)


@pytest.mark.parametrize("task_type", TASK_TYPES)
@pytest.mark.parametrize("status", ["todo", "in_progress", "needs_review", "done"])
@pytest.mark.parametrize("stage", ["", "nonsense", "Drawing", "Inspect", "Lead review", "AI pre-check"])
def test_the_position_always_agrees_with_the_status(task_type, status, stage):
    t = task(task_type, status, stage)
    listed = steps.steps_for(t)
    here = steps.current_index(t, listed)

    if status == "done":
        assert here == len(listed)
    elif status == "needs_review":
        assert listed[here].review
    else:
        assert not listed[here].review
    # the strip marks exactly one current step, unless the task is finished
    states = [s["state"] for s in steps.strip(t)]
    assert states.count("current") == (0 if status == "done" else 1)


def test_a_done_task_has_every_step_past():
    assert {s["state"] for s in steps.strip(task(status="done", stage="Inspect"))} == {"past"}


def test_custom_steps_replace_the_defaults():
    t = task(custom=[{"name": "Order stock", "review": False}, {"name": "Sign off", "review": True}])

    assert [s.name for s in steps.steps_for(t)] == ["Order stock", "Sign off"]


def test_a_damaged_custom_list_falls_back_to_the_defaults():
    for bad in ([], [{"name": ""}], ["x"], [{"review": True}]):
        assert [s.name for s in steps.steps_for(task(custom=bad))] == names("Fabricate")


def test_changing_type_keeps_a_step_of_the_same_name_and_the_review_wait():
    waiting = task("Fabricate", "needs_review", "Lead review")
    assert steps.retype(waiting, "Design") == ("Lead review", "needs_review")
    fit = task("Design", "in_progress", "Fit check")
    assert steps.retype(fit, "Print") == ("Fit check", "in_progress")


def test_changing_type_with_custom_steps_changes_nothing_else():
    t = task(custom=[{"name": "A", "review": False}, {"name": "B", "review": True}], stage="A")

    assert steps.retype(t, "Print") == ("A", "in_progress")


def test_unknown_type_cannot_be_chosen():
    with pytest.raises(ValueError):
        steps.retype(task(), "Paint")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Print the example lid latch", "Print"),
        ("3D printed gasket jig", "Print"),
        ("Research example camera housings", "Investigate"),
        ("Decide on the example fastener", "Investigate"),
        ("Test example lid fit", "Validate"),
        ("Make example side panels", "Fabricate"),
    ],
)
def test_import_guesses_the_new_types(title, expected):
    assert import_tasks.guess_task_type(title) == expected


@pytest.mark.parametrize(
    ("task_type", "status", "stage", "expected"),
    [
        ("Fabricate", "in_progress", "AI pre-check", "Drawing"),
        ("Fabricate", "needs_review", "shop", "Lead review"),
        ("Validate", "todo", "", "Test plan"),
        ("Validate", "done", "run", "Log results"),
        ("Print", "in_progress", "Lead review", "Check CAD"),
    ],
)
def test_import_stores_a_stage_that_agrees_with_the_status(task_type, status, stage, expected):
    assert steps.fit_stage(task_type, status, stage) == expected


def test_migration_moves_stages_to_agree_with_status_and_drops_ai_pre_check(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    command.upgrade(db.alembic_config(engine), "0009")
    rows = [
        (1, "Fabricate", "in_progress", "AI pre-check"),
        (2, "Fabricate", "needs_review", "drawing"),
        (3, "Validate", "in_progress", "Calculations"),
        (4, "Design", "done", "cad"),
        (5, "Design", "in_progress", "concepts"),
    ]
    with engine.begin() as conn:
        for id_, kind, status, stage in rows:
            conn.execute(
                text(
                    "INSERT INTO task (id, code, title, subsystem, task_type, stage, status) "
                    "VALUES (:i, :c, 't', 'Enclosure', :k, :g, :s)"
                ),
                {"i": id_, "c": f"M0{id_}", "k": kind, "g": stage, "s": status},
            )

    command.upgrade(db.alembic_config(engine), "head")

    with engine.connect() as conn:
        stages = dict(conn.execute(text("SELECT id, stage FROM task")).all())
    engine.dispose()
    assert stages == {1: "Drawing", 2: "Lead review", 3: "Test plan", 4: "Fit check", 5: "Concepts"}
