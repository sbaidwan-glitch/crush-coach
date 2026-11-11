"""The Jinja macros in templates/components/ render what the design rules require."""

import re

import pytest

import main
from models import DECISION_STATUS_LABELS, STATUS_LABELS, Decision, Task


def macros(name: str):
    return main.templates.env.get_template(f"components/{name}.html").module


def test_status_label_is_icon_plus_words_for_every_status():
    status = macros("status")

    for value, label in STATUS_LABELS.items():
        html = str(status.status_label(value, label))
        assert "<svg" in html and 'aria-hidden="true"' in html  # an icon
        assert f"<span>{label}</span>" in html  # and words
        assert f"status-{value}" in html


def test_decision_statuses_are_icon_plus_words_and_each_icon_differs():
    status = macros("status")
    icons = {}

    for value, label in DECISION_STATUS_LABELS.items():
        html = str(status.status_label(value, label))
        assert "<svg" in html and 'aria-hidden="true"' in html
        assert f"<span>{label}</span>" in html and f"status-{value}" in html
        icons[value] = html.split("<span>")[0]
    assert len(set(icons.values())) == len(icons)  # never the same shape for two statuses


def test_decision_card_links_to_the_decision_with_status_and_subsystem():
    d = Decision(
        id=7,
        subsystem="Enclosure",
        topic="Latch style",
        decision="Use the cam latch.",
        rationale="r",
        status="confirmed",
        proposed_by="sam@example.edu",
    )

    html = str(macros("decision_card").decision_card(d, DECISION_STATUS_LABELS))

    assert '<a href="/decisions/7">Latch style</a>' in html
    assert "Confirmed" in html and "Enclosure" in html and "Use the cam latch." in html


def test_error_notice_is_an_alert_and_a_note_is_a_status():
    feedback = macros("feedback")

    error = str(feedback.notice("It broke.", "error"))
    note = str(feedback.notice("FYI.", "note"))

    assert 'role="alert"' in error and "<svg" in error and "It broke." in error
    assert 'role="status"' in note and "<svg" in note


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


def test_button_variants_and_link():
    button = macros("button")

    assert 'class="btn btn-coach"' in str(button.button("Ask", "coach", "submit"))
    assert 'type="submit"' in str(button.button("Ask", "coach", "submit"))
    link = str(button.button_link("/login", "Sign in"))
    assert '<a class="btn btn-primary" href="/login">Sign in</a>' in link


def test_readout_marks_wide_and_muted_cells_and_escapes_values():
    html = str(
        macros("readout").readout(
            [
                {"label": "Subsystem", "value": "<b>x</b>", "wide": True},
                {"label": "Owner", "value": "Unclaimed", "muted": True},
            ]
        )
    )

    assert '<div class="wide"><dt>Subsystem</dt>' in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert '<dd class="muted">Unclaimed</dd>' in html


def test_gauge_has_one_tick_per_task_and_words_for_the_numbers():
    html = str(macros("gauge").gauge(3, 8))

    assert "3 of 8 done" in html
    assert len(re.findall(r'class="tick(?: on)?"', html)) == 8
    assert html.count('class="tick on"') == 3
    assert (
        'aria-hidden="true"' in html
    )  # the ticks are decoration; the words carry the data


@pytest.mark.parametrize(
    ("owner", "expected"), [("Sam Example", "Sam Example"), (None, "Unclaimed")]
)
def test_task_card_links_to_the_task_and_names_the_owner(owner, expected):
    task = Task(
        code="M04",
        title="<i>Panels</i>",
        subsystem="Enclosure",
        task_type="Fabricate",
        stage="shop",
        owner=owner,
        status="in_progress",
    )

    html = str(macros("task_card").task_card(task, STATUS_LABELS))

    assert '<a href="/tasks/M04">' in html
    assert "&lt;i&gt;Panels&lt;/i&gt;" in html
    assert expected in html
    assert "In progress" in html and "<svg" in html
    assert "Owner: " in html  # the owner is labelled for screen readers
    assert "Stage" not in html  # the board card is compact; stage is on the task page
