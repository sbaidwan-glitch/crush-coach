"""The Jinja macros in templates/components/ render what docs/design.md requires."""

import re

import pytest

import main
from models import DECISION_STATUS_LABELS, STATUS_LABELS, Decision, Task
from progress import group_progress


def macros(name: str):
    return main.templates.env.get_template(f"components/{name}.html").module


def test_status_label_has_its_own_mark_and_the_words_for_every_status():
    status = macros("status")
    marks = {}

    for value, label in STATUS_LABELS.items():
        html = str(status.status_label(value, label))
        assert f">{label}</span>" in html  # always words, never colour alone
        marks[value] = re.search(r"status--([\w-]+)", html).group(1)
    # Open circle, half circle, filled square, filled circle: four different shapes.
    assert marks == {
        "todo": "todo",
        "in_progress": "in-progress",
        "needs_review": "review",
        "done": "done",
    }


def test_decision_statuses_reuse_the_marks_with_their_words():
    status = macros("status")

    for value, label in DECISION_STATUS_LABELS.items():
        html = str(status.status_label(value, label))
        assert f">{label}</span>" in html and "status--" in html
    assert "status--review" in str(status.status_label("proposed", "Proposed"))
    assert "status--done" in str(status.status_label("confirmed", "Confirmed"))


def make_decision(status="confirmed", **kwargs):
    return Decision(
        id=7,
        subsystem="Enclosure",
        topic="Latch style",
        decision="Use the cam latch.",
        rationale="r",
        status=status,
        proposed_by="sam@example.edu",
        **kwargs,
    )


def test_decision_row_links_to_the_decision_with_status_and_subsystem():
    html = str(macros("decision_row").decision_row(make_decision(), DECISION_STATUS_LABELS))

    assert '<a href="/decisions/7">Latch style</a>' in html
    assert ">D7</span>" in html
    assert "Confirmed" in html and "Enclosure" in html


def test_a_proposed_decision_says_it_needs_a_lead():
    row = macros("decision_row").decision_row
    html = str(row(make_decision("proposed"), DECISION_STATUS_LABELS, summary=True))

    assert "Proposed · needs a lead" in html
    assert "Use the cam latch." in html  # the summary, when asked for


def test_error_notice_is_an_alert_and_a_note_is_a_status():
    feedback = macros("feedback")

    error = str(feedback.notice("It broke.", "error"))
    note = str(feedback.notice("FYI.", "note"))

    assert 'role="alert"' in error and "notice--error" in error and "It broke." in error
    assert 'role="status"' in note and "notice--error" not in note


def test_empty_and_error_states_offer_an_action_only_when_given():
    feedback = macros("feedback")

    plain = str(feedback.empty_state("Nothing here."))
    with_action = str(feedback.error_state("Failed.", "/tasks", "Try again"))

    assert "Nothing here." in plain and "<a " not in plain
    assert 'role="alert"' in with_action
    assert '<a href="/tasks">Try again</a>' in with_action


def test_loading_indicator_is_a_labelled_status():
    html = str(macros("feedback").loading_indicator("busy", "Thinking…"))

    assert 'id="busy"' in html and 'role="status"' in html
    assert "htmx-indicator" in html and "Thinking…" in html


def test_button_variants_sizes_and_link():
    button = macros("button")

    primary = str(button.button("Ask", "primary", "submit"))
    assert 'class="btn btn--primary"' in primary and 'type="submit"' in primary
    assert 'class="btn"' in str(button.button("Cancel"))  # secondary is outlined
    assert 'class="btn btn--small"' in str(button.button("Go", small=True))
    link = str(button.button_link("/login", "Sign in"))
    assert '<a class="btn btn--primary" href="/login">Sign in</a>' in link


def test_readout_shows_labelled_fields_dims_muted_ones_and_escapes_values():
    html = str(
        macros("readout").readout(
            [
                {"label": "Subsystem", "value": "<b>x</b>"},
                {"label": "Owner", "value": "Unclaimed", "muted": True},
            ]
        )
    )

    assert '<div class="field-label">Subsystem</div>' in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert '<div class="field meta">Unclaimed</div>' in html


def task(**kwargs):
    values = {
        "code": "M04",
        "title": "Panels",
        "subsystem": "Enclosure",
        "task_type": "Fabricate",
        "stage": "shop",
        "status": "in_progress",
    }
    return Task(**{**values, **kwargs})


def test_progress_bar_is_one_ruler_with_the_numbers_in_words():
    tasks = [task(code="A", status="done"), task(code="B"), task(code="C", status="todo")]
    group = group_progress(tasks)[0]

    html = str(macros("progress_bar").pbar(group, flex=True))

    assert 'class="pbar"' in html and 'role="img"' in html
    assert 'aria-label="Enclosure: 1 of 3 done, 1 in progress"' in html
    assert "--n:3" in html and "--c:var(--color-sub-1)" in html
    assert "--done:33.33%" in html and "--started:66.67%" in html
    assert "flex:3" in html


@pytest.mark.parametrize(
    ("owner", "expected"), [("Sam Example", "Sam Example"), (None, "Unclaimed")]
)
def test_task_row_links_to_the_task_and_names_the_owner(owner, expected):
    row = macros("task_row").task_row(
        task(title="<i>Panels</i>", owner=owner), STATUS_LABELS
    )
    html = str(row)

    assert '<a class="task-row__title" href="/tasks/M04">' in html
    assert "&lt;i&gt;Panels&lt;/i&gt;" in html
    assert expected in html
    assert "Owner: " in html  # the owner is labelled for screen readers
    assert "In progress" in html and "status--in-progress" in html
    assert ("unclaimed" in html) == (owner is None)


def test_claim_row_posts_a_start_and_comes_back_to_the_page_it_was_on():
    html = str(macros("task_row").claim_row(task(status="todo"), "/"))

    assert 'action="/tasks/M04/status"' in html and 'method="post"' in html
    assert 'name="action" value="start"' in html
    assert 'name="next" value="/"' in html
    assert "Claim" in html and "Enclosure · Fabricate" in html
