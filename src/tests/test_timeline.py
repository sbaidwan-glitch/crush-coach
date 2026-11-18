"""docs/timeline.md: the Timeline's bars come from TaskEvent, the tone per part of Crush,
who is shown at a bar's tip, and the sign-in rule."""

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import auth
import localtime
import main
import progress
import timeline
from models import MemberProfile, Task, TaskEvent
from timeline import Moment, Span, span_for

MEMBER = {"name": "Riley Sample", "email": "member@example.edu", "role": "member"}
TODAY = date(2026, 11, 17)  # a Tuesday


def stored_utc(year, month, number, hour) -> datetime:
    """A moment as the database keeps it: UTC, without a time zone."""
    return datetime(year, month, number, hour, tzinfo=UTC).replace(tzinfo=None)


def day(month: int, number: int) -> date:
    return date(2026, month, number)


def m(kind, to_status, when, from_status=None) -> Moment:
    return Moment(kind, from_status, to_status, when)


# --- span_for: the date rules, no database ---


def test_an_open_task_runs_from_its_start_to_today():
    events = [m("start", "in_progress", day(10, 12), "todo")]

    assert span_for("in_progress", events, TODAY) == Span(day(10, 12), TODAY, "open")


def test_a_finished_task_runs_from_its_start_to_its_done_event():
    events = [
        m("start", "in_progress", day(10, 5), "todo"),
        m("advance", "needs_review", day(10, 20), "in_progress"),
        m("approve", "in_progress", day(10, 22), "needs_review"),
        m("advance", "done", day(10, 30), "in_progress"),
    ]

    assert span_for("done", events, TODAY) == Span(day(10, 5), day(10, 30), "done")


def test_unclaiming_and_picking_it_up_again_starts_the_bar_again():
    events = [
        m("start", "in_progress", day(10, 1), "todo"),
        m("unclaim", "todo", day(10, 5), "in_progress"),
        m("start", "in_progress", day(10, 14), "todo"),
    ]

    assert span_for("in_progress", events, TODAY) == Span(day(10, 14), TODAY, "open")


def test_a_task_that_was_unclaimed_and_not_picked_up_has_no_bar():
    events = [
        m("start", "in_progress", day(10, 1), "todo"),
        m("unclaim", "todo", day(10, 5), "in_progress"),
    ]

    assert span_for("todo", events, TODAY) is None


def test_a_task_waiting_for_review_ends_the_day_it_was_sent_for_review():
    events = [
        m("start", "in_progress", day(10, 19), "todo"),
        m("advance", "needs_review", day(11, 13), "in_progress"),
    ]

    assert span_for("needs_review", events, TODAY) == Span(day(10, 19), day(11, 13), "review")


def test_a_task_sent_back_is_open_again_and_keeps_its_original_start():
    events = [
        m("start", "in_progress", day(10, 19), "todo"),
        m("advance", "needs_review", day(11, 3), "in_progress"),
        m("send_back", "in_progress", day(11, 6), "needs_review"),
    ]

    assert span_for("in_progress", events, TODAY) == Span(day(10, 19), TODAY, "open")


def test_a_task_sent_for_review_twice_ends_at_the_latest_request():
    events = [
        m("start", "in_progress", day(10, 19), "todo"),
        m("advance", "needs_review", day(11, 3), "in_progress"),
        m("send_back", "in_progress", day(11, 6), "needs_review"),
        m("advance", "needs_review", day(11, 12), "in_progress"),
    ]

    assert span_for("needs_review", events, TODAY).end == day(11, 12)


def test_a_task_with_no_events_has_no_bar_whatever_its_status():
    for status in ("todo", "in_progress", "needs_review", "done"):
        assert span_for(status, [], TODAY) is None


def test_a_task_in_progress_before_events_began_starts_at_its_first_event():
    events = [m("advance", "in_progress", day(11, 2), "in_progress")]

    assert span_for("in_progress", events, TODAY) == Span(day(11, 2), TODAY, "open")


def test_a_done_task_with_no_done_event_ends_at_its_last_event():
    events = [m("advance", "in_progress", day(11, 2), "in_progress"),
              m("change_type", "in_progress", day(11, 9), "in_progress")]

    assert span_for("done", events, TODAY) == Span(day(11, 2), day(11, 9), "done")


def test_rows_from_before_events_had_a_kind_still_say_when_it_started():
    events = [m(None, "in_progress", day(10, 8), "todo")]

    assert span_for("in_progress", events, TODAY).start == day(10, 8)


def test_a_start_after_the_finish_never_draws_a_backwards_bar():
    events = [m("advance", "done", day(10, 1), "in_progress"),
              m("start", "in_progress", day(10, 9), "todo")]

    span = span_for("done", events, TODAY)

    assert span.start <= span.end


def test_a_task_not_started_has_no_bar_even_with_events():
    assert span_for("todo", [m("change_type", "todo", day(10, 1))], TODAY) is None


# --- the axis ---


def test_the_axis_starts_on_a_monday_a_week_before_the_earliest_bar():
    axis = timeline.axis_for([Span(day(10, 14), TODAY, "open")], TODAY)

    assert axis.start == day(10, 5)
    assert axis.start.weekday() == 0
    assert (axis.columns, axis.column_days, axis.days) == (8, 7, 56)  # eight weeks, a column each


def test_the_axis_always_includes_today_and_a_week_after_the_latest_end():
    axis = timeline.axis_for([Span(day(10, 1), day(10, 10), "done")], TODAY)

    assert 0 < axis.pct(TODAY) < 100
    assert axis.pct(TODAY + timedelta(days=7)) <= 100


def test_a_long_season_uses_whole_week_columns_not_a_squashed_chart():
    axis = timeline.axis_for([Span(day(1, 5), TODAY, "open")], TODAY)

    assert axis.column_days % 7 == 0 and axis.column_days > 7
    assert axis.pct(TODAY) < 100
    labels = [label for _, label in axis.ticks()]
    assert labels[0] == "Dec 29" and len(labels) == axis.columns <= timeline.MAX_COLUMNS


def test_a_range_a_little_over_eight_weeks_still_gets_a_column_per_week():
    # 57 days from the Monday before the earliest start to a week after today.
    axis = timeline.axis_for([Span(day(10, 10), TODAY, "open")], TODAY)

    assert axis.column_days == 7 and axis.columns == 9
    labels = [label for _, label in axis.ticks()]
    assert labels[:3] == ["Sep 28", "Oct 5", "Oct 12"]
    assert axis.column_pct == 11.11


def test_a_young_project_is_not_stretched_wider_than_eight_weeks_of_columns():
    axis = timeline.axis_for([Span(day(11, 10), TODAY, "open")], TODAY)

    assert axis.columns == timeline.MIN_WEEKS


def test_with_no_bars_the_axis_is_built_around_today():
    axis = timeline.axis_for([], TODAY)

    assert 0 < axis.pct(TODAY) < 100


def test_percentages_match_the_designs_reference_chart():
    axis = timeline.axis_for([Span(day(10, 12), TODAY, "open")], TODAY)

    assert axis.pct(day(10, 12)) == 12.5
    assert axis.pct(TODAY) == 76.79
    assert axis.width_pct(day(10, 12), TODAY) == 64.29
    assert axis.width_pct(TODAY, TODAY) == round(100 / 56, 2)  # a one-day task is still visible


# --- the page ---


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(localtime, "today", lambda: TODAY)
    main.app.dependency_overrides[auth.require_user] = lambda: MEMBER
    main.app.dependency_overrides[auth.optional_user] = lambda: MEMBER
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def add_task(session, code, subsystem, status="todo", owner=None, title=None):
    task = Task(code=code, title=title or f"Task {code}", subsystem=subsystem,
                task_type="Fabricate", stage="drawing", status=status, owner=owner)
    session.add(task)
    session.commit()
    return task


def add_event(session, task, kind, to_status, when, from_status=None):
    """`when` is a local day; the event is stored at noon UTC (the same local day)."""
    session.add(TaskEvent(task_id=task.id, actor="Someone", kind=kind, from_status=from_status,
                          to_status=to_status,
                          created_at=stored_utc(when.year, when.month, when.day, 20)))
    session.commit()


def panel_html(html: str, slug: str) -> str:
    start = html.index(f'aria-labelledby="h-{slug}"')
    return html[start : html.index("</section>", start)]


def test_each_part_of_crush_is_a_panel_in_its_own_tone(session, client):
    add_task(session, "M01", "Enclosure")
    add_task(session, "E01", "Electronics Mounts")
    add_task(session, "M10", "Seals/Waterproofing")
    add_task(session, "M20", "Thrusters")
    add_task(session, "G01", "Gripper")
    add_task(session, "T01", "Torpedo")

    html = client.get("/timeline").text

    for slug in ("enclosure", "seals", "thrusters", "actuators"):
        tone = progress.PANEL_TONES[slug]
        assert re.search(rf'<section class="panel panel--flush" data-tone="{tone}" aria-labelledby="h-{slug}"', html)
    assert [progress.PANEL_TONES[s] for s in ("enclosure", "seals", "thrusters", "actuators")] == [
        "blue", "teal", "sage", "sand"]  # the same colours as their progress bars
    # Electronics Mounts sit in the Enclosure panel; Gripper and Torpedo in Actuators.
    assert "E01" in panel_html(html, "enclosure")
    assert "G01" in panel_html(html, "actuators") and "T01" in panel_html(html, "actuators")


def test_a_bar_is_drawn_from_events_with_the_owners_chip_at_its_tip(session, client):
    task = add_task(session, "M05", "Enclosure", "in_progress", owner="Riley Sample",
                    title="Cut the lid")
    add_event(session, task, "start", "in_progress", day(10, 12), "todo")
    session.add(MemberProfile(email=MEMBER["email"], icon="octopus", display_name="Rye",
                              full_name="Riley Sample"))
    session.commit()

    html = client.get("/timeline").text

    row = panel_html(html, "enclosure")
    assert 'tl-bar tl-bar--in-progress" style="left:12.5%;width:64.29%"' in row
    assert 'aria-label="Cut the lid: Rye · Oct 12 – today"' in row
    assert 'class="tl-who" style="left:76.79%"' in row
    assert "#av-octopus" in row and ">Rye<" in row
    assert 'style="--today:76.79%"' in html and 'data-today="76.79"' in html
    assert 'class="tl-inner" style="--col:12.5%"' in html  # the grid lines: one per week


def test_bar_styles_say_finished_open_or_waiting_for_review(session, client):
    done = add_task(session, "M01", "Enclosure", "done")
    add_event(session, done, "start", "in_progress", day(10, 6), "todo")
    add_event(session, done, "advance", "done", day(10, 30), "in_progress")
    waiting = add_task(session, "M02", "Enclosure", "needs_review")
    add_event(session, waiting, "start", "in_progress", day(10, 19), "todo")
    add_event(session, waiting, "advance", "needs_review", day(11, 13), "in_progress")

    row = panel_html(client.get("/timeline").text, "enclosure")

    assert "tl-bar tl-bar--done" in row and "tl-bar tl-bar--review" in row
    assert "Done" in row and "Needs review" in row  # the words, never colour alone
    assert "Oct 19 – Nov 13" in row


def test_tasks_with_no_bar_are_named_at_the_foot_of_their_panel(session, client):
    add_task(session, "M08", "Enclosure")  # not started
    add_task(session, "M09", "Enclosure", "in_progress", owner="Sam Example")  # no events at all

    row = panel_html(client.get("/timeline").text, "enclosure")

    assert "Not started, no bar yet: M08" in row
    assert "Started before the tracker kept dates, no bar: M09" in row
    assert "tl-bar " not in row


def test_two_owners_stack_two_chips_and_say_plus_one(session, client):
    task = add_task(session, "M12", "Seals/Waterproofing", "in_progress",
                    owner="Sam Example, Riley Sample")
    add_event(session, task, "start", "in_progress", day(11, 2), "todo")

    row = panel_html(client.get("/timeline").text, "seals")

    assert row.count('class="avatar"') == 2 and ">Sam +1<" in row


def test_signed_out_visitors_get_the_sign_in_page_and_nothing_from_the_database(session):
    add_task(session, "M01", "Enclosure", title="A secret title")
    main.app.dependency_overrides.clear()

    response = TestClient(main.app).get("/timeline")

    assert response.status_code == 401
    assert "Sign in to see the timeline" in response.text
    assert "A secret title" not in response.text and "M01" not in response.text


def test_no_tasks_says_so_instead_of_drawing_an_empty_chart(session, client):
    html = client.get("/timeline").text

    assert "No tasks yet" in html and 'class="tl"' not in html


def test_a_database_failure_is_said_plainly_with_the_rest_of_the_page(session, client, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("boom with secret@host")

    monkeypatch.setattr(timeline, "load_events", fail)
    add_task(session, "M01", "Enclosure")

    response = client.get("/timeline")

    assert response.status_code == 503
    assert "Couldn&#39;t load the timeline" in response.text or "Couldn't load the timeline" in response.text
    assert "secret@host" not in response.text
    assert 'href="/timeline"' in response.text  # the sidebar is still there


def test_the_timeline_link_is_in_the_sidebar_and_marked_current(session, client):
    html = client.get("/timeline").text

    nav = html[html.index('<nav class="sidebar"') : html.index("</nav>")]
    assert re.search(r'<a class="sidebar__link" href="/timeline" aria-current="page">.*?<span>Timeline</span>', nav)
    assert nav.index("/tasks") < nav.index("/timeline") < nav.index("/record")


def test_every_task_start_and_finish_uses_the_teams_local_day(session, client):
    task = add_task(session, "M01", "Enclosure", "done")
    # 03:00 UTC on 13 Oct is still 12 Oct in Los Angeles.
    session.add(TaskEvent(task_id=task.id, actor="x", kind="start", from_status="todo",
                          to_status="in_progress", created_at=stored_utc(2026, 10, 13, 3)))
    session.add(TaskEvent(task_id=task.id, actor="x", kind="advance", from_status="in_progress",
                          to_status="done", created_at=stored_utc(2026, 10, 20, 20)))
    session.commit()

    assert "Oct 12 – Oct 20" in client.get("/timeline").text
