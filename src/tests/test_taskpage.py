"""Presentation helpers for the task page and Home: stages, Next step, message parts."""

from datetime import timedelta

import pytest

import taskpage
from models import CoachMessage, Task, TaskEvent, utc_now


def task(**kwargs):
    values = {
        "code": "M04",
        "title": "t",
        "subsystem": "Enclosure",
        "task_type": "Design",
        "stage": "CAD",
    }
    return Task(**{**values, **kwargs})


def test_stage_strip_follows_the_task_type_and_marks_past_and_current():
    strip = taskpage.stage_strip(task())

    assert [s["label"] for s in strip] == [
        "Requirements",
        "Concepts",
        "Lead review",
        "CAD",
        "Fit check",
    ]
    assert [s["state"] for s in strip] == ["past", "past", "past", "current", "ahead"]


@pytest.mark.parametrize(
    ("task_type", "stage", "current"),
    [
        ("Fabricate", "shop", "Shop"),
        ("Fabricate", "Shop (machine)", "Shop"),
        ("Fabricate", "AI pre-check", "AI pre-check"),
        ("Validate", "test plan", "Test plan"),
        ("Validate", "log results", "Log results"),
    ],
)
def test_stage_words_match_however_they_are_stored(task_type, stage, current):
    strip = taskpage.stage_strip(task(task_type=task_type, stage=stage))

    assert [s["label"] for s in strip if s["state"] == "current"] == [current]


def test_an_unknown_stage_marks_nothing_instead_of_guessing():
    strip = taskpage.stage_strip(task(stage="somewhere else"))

    assert strip and {s["state"] for s in strip} == {"ahead"}


def test_an_unknown_task_type_has_no_strip():
    assert taskpage.stage_strip(task(task_type="Other")) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Answer.\n\nNext step: open the assembly.", "Open the assembly."),
        ("Answer.\n\n**Next step:** open the **assembly**.", "Open the assembly."),
        ("- Next step: check [the sheet](https://docs.google.com/x) first", "Check the sheet first"),
        ("next STEP :   spaced out", "Spaced out"),
        ("Next step: first\nNext step: last one wins", "Last one wins"),
        ("No such line here.", None),
        ("Next step:", None),
        ("The next steps are many.", None),
        ("", None),
    ],
)
def test_next_step_line_is_read_from_the_answer(text, expected):
    assert taskpage.next_step_from(text) == expected


def test_latest_next_step_comes_from_the_newest_answer_only(session):
    t = task(code="M04")
    session.add(t)
    session.commit()
    now = utc_now()
    for age, content in (
        (3, "Older.\n\nNext step: an old step."),
        (1, "Newest answer with no step."),
    ):
        session.add(
            CoachMessage(
                task_id=t.id,
                member="Coach",
                role="assistant",
                content=content,
                created_at=now - timedelta(days=age),
            )
        )
    session.commit()

    # The newest answer has no step: an older answer's step may be out of date.
    assert taskpage.latest_next_steps(session, [t.id]) == {}

    session.add(
        CoachMessage(
            task_id=t.id,
            member="Coach",
            role="assistant",
            content="Fresh.\n\nNext step: do the new thing.",
            created_at=now,
        )
    )
    session.commit()
    found = taskpage.latest_next_steps(session, [t.id])
    assert found[t.id]["text"] == "Do the new thing."


def test_a_members_own_message_never_counts_as_a_next_step(session):
    t = task()
    session.add(t)
    session.commit()
    session.add(CoachMessage(task_id=t.id, member="x", role="user", content="Next step: me"))
    session.commit()

    assert taskpage.latest_next_steps(session, [t.id]) == {}


def test_message_parts_split_out_the_next_step_and_the_conflict():
    text = (
        "Start from the current file.\n\nAlso check the sheet.\n\n"
        "**Likely current: 0.050\"** (Docs, Mar 2026). Older: 0.030\". A lead should confirm.\n\n"
        "Next step: open the sub-assembly."
    )

    parts = taskpage.message_parts(text)

    assert [p["kind"] for p in parts] == ["body", "conflict", "next"]
    assert parts[0]["text"] == "Start from the current file.\n\nAlso check the sheet."
    assert parts[1]["text"].startswith("**Likely current")
    assert parts[2]["text"] == "open the sub-assembly."


def test_an_answer_without_either_is_one_body_exactly_as_written():
    text = "One paragraph.\n\n- a list\n- of things"

    assert taskpage.message_parts(text) == [{"kind": "body", "text": text}]


def test_a_next_step_on_the_last_line_of_a_paragraph_is_still_split_out():
    parts = taskpage.message_parts("Do this first.\nNext step: then that.")

    assert parts == [
        {"kind": "body", "text": "Do this first."},
        {"kind": "next", "text": "then that."},
    ]


def test_history_text_says_who_did_what():
    def line(from_status, to_status, actor="Sam"):
        return taskpage.event_text(
            TaskEvent(task_id=1, actor=actor, from_status=from_status, to_status=to_status)
        )

    assert line("todo", "in_progress") == "Sam started the task"
    assert line("in_progress", "needs_review") == "Sam sent it for review"
    assert line("needs_review", "done") == "Sam approved it"
    assert line("needs_review", "in_progress") == "Sam sent it back"
    assert line("in_progress", "todo") == "Sam unclaimed the task"
