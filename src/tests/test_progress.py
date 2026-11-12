"""Build progress: every number on Home and Tasks comes from these."""

import pytest

import progress
from models import STATUSES, Task, normalize_status


def task(code, subsystem="Enclosure", status="todo", owner=None):
    return Task(
        code=code,
        title=code,
        subsystem=subsystem,
        task_type="Design",
        stage="x",
        status=status,
        owner=owner,
    )


def test_four_parts_of_crush_in_order_with_the_subsystems_folded_in():
    groups = progress.group_progress(
        [
            task("a", "Electronics Mounts"),
            task("b", "Gripper"),
            task("c", "Torpedo"),
            task("d", "Seals/Waterproofing"),
        ]
    )

    assert [(g.slug, g.total) for g in groups] == [
        ("enclosure", 1),
        ("seals", 1),
        ("thrusters", 0),
        ("actuators", 2),
    ]
    assert [g.colour for g in groups] == [1, 2, 3, 4]


def test_done_and_started_percentages_count_review_as_started():
    [group, *_] = progress.group_progress(
        [
            task("a", status="done"),
            task("b", status="in_progress"),
            task("c", status="needs_review"),
            task("d"),
        ]
    )

    assert (group.total, group.done, group.in_progress, group.open) == (4, 1, 2, 3)
    assert (group.done_pct, group.started_pct) == (25, 75)
    assert group.aria_label == "Enclosure: 1 of 4 done, 2 in progress"


def test_an_empty_part_does_not_divide_by_zero():
    group = progress.group_progress([])[0]

    assert (group.done_pct, group.started_pct, group.tag_state) == (0, 0, "todo")


@pytest.mark.parametrize(
    ("statuses", "state"),
    [
        (["done", "done"], "done"),
        (["done", "todo"], "in_progress"),
        (["in_progress", "todo"], "in_progress"),
        (["needs_review"], "in_progress"),
        (["todo", "todo"], "todo"),
    ],
)
def test_the_tag_on_crush_is_done_started_or_not_started(statuses, state):
    tasks = [task(str(i), status=s) for i, s in enumerate(statuses)]

    assert progress.group_progress(tasks)[0].tag_state == state


def test_overall_total_and_rounded_percent():
    groups = progress.group_progress(
        [task("a", status="done"), task("b"), task("c", "Thrusters")]
    )

    assert progress.overall(groups) == {"total": 3, "done": 1, "percent": 33}
    assert progress.overall([]) == {"total": 0, "done": 0, "percent": 0}


def test_counts_for_the_box_on_the_tasks_page():
    counts = progress.counts(
        [
            task("a", status="done"),
            task("b", status="in_progress", owner="X"),
            task("c", status="needs_review", owner="Y"),
            task("d"),
            task("e", owner="Z"),  # assigned but not started: not unclaimed
        ]
    )

    assert counts == {"total": 5, "done": 1, "in_progress": 1, "review": 1, "unclaimed": 1}


def test_owned_by_matches_name_or_email_and_never_an_empty_owner():
    t = task("a", owner=" Sam Example ")

    assert progress.owned_by(t, "sam example", "sam@example.edu")
    assert not progress.owned_by(t, "Alex", "alex@example.edu")
    assert not progress.owned_by(task("b"), "", "")


@pytest.mark.parametrize(
    ("word", "status"),
    [
        ("todo", "todo"),
        ("In Review", "needs_review"),
        ("needs review", "needs_review"),
        ("  Complete ", "done"),
        ("not started", "todo"),
        ("In progress", "in_progress"),
        ("blorp", None),
        ("", None),
        (None, None),
    ],
)
def test_imported_status_words_map_to_the_flow(word, status):
    assert normalize_status(word) == status
    assert status is None or status in STATUSES
