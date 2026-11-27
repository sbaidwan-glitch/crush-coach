"""Recorded answers: the Record page, recording and replacing, the old /decisions links, and how
the coach uses them. Made-up data only."""

from datetime import timedelta

import fake_openai as fake
import pytest
from fastapi.testclient import TestClient
from index_helpers import add_file
from sqlmodel import select

import auth
import coach
import decisions
import drive
import llm
import main
from models import Decision, DecisionTask, LlmCall, Task, utc_now

LINK = "https://docs.google.com/document/d/1ExampleLatchNotes0001/edit"
MEMBER = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}
LEAD = {"name": "Lee Lead", "email": "lee@example.edu", "role": "lead"}


def sign_in_as(user):
    main.app.dependency_overrides[auth.require_user] = lambda: user
    main.app.dependency_overrides[auth.optional_user] = lambda: user


@pytest.fixture
def client():
    sign_in_as(MEMBER)
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def lead_client(client):
    sign_in_as(LEAD)
    return client


@pytest.fixture
def task(session):
    session.add(
        Task(
            code="M04",
            title="Finish enclosure wall panels",
            subsystem="Enclosure",
            task_type="Fabricate",
            stage="drawing",
        )
    )
    session.commit()
    return session.exec(select(Task)).one()


def make_decision(session, **overrides) -> Decision:
    values = {
        "subsystem": "Enclosure",
        "topic": "Latch style",
        "decision": "Use the two-point cam latch.",
        "rationale": "It seals evenly and a diver can open it with one hand.",
        "alternatives": [
            {"option": "Toggle latch", "rejected_because": "Loosens under vibration."}
        ],
        "sources": [
            {
                "title": "Latch design notes",
                "link": LINK,
                "folder": "Crush / Enclosure",
                "year": 2026,
            }
        ],
        "status": "proposed",
        "proposed_by": "sam@example.edu",
    }
    values.update(overrides)
    decision = Decision(**values)
    session.add(decision)
    session.commit()
    session.refresh(decision)
    return decision


def confirmed(session, **overrides) -> Decision:
    values = {"status": "confirmed", "confirmed_by": "lee@example.edu", "confirmed_at": utc_now()}
    return make_decision(session, **{**values, **overrides})


def stored(session, model):
    session.expire_all()
    return list(session.exec(select(model)).all())



# --- recording an answer ---


def test_only_a_lead_can_record_an_answer(session, task):
    with pytest.raises(decisions.RecordError):
        decisions.record_answer(
            session, MEMBER, subsystem="Enclosure", topic="Latch", answer="Cam latch"
        )

    assert stored(session, Decision) == []


def test_a_recorded_answer_is_confirmed_and_linked_to_its_task(session, task):
    decision = decisions.record_answer(
        session, LEAD, subsystem="Enclosure", topic="Latch", answer="Cam latch", task=task
    )
    session.commit()

    saved = stored(session, Decision)[0]
    assert saved.status == "confirmed" and saved.confirmed_by == "Lee Lead"
    assert decisions.task_codes_for(session, decision.id) == ["M04"]


def test_a_newer_answer_to_the_same_question_replaces_the_old_one(session, task):
    first = decisions.record_answer(
        session, LEAD, subsystem="Enclosure", topic="Gasket gap", answer="0.030 in",
        replace_same_topic=True,
    )
    other = decisions.record_answer(
        session, LEAD, subsystem="Enclosure", topic="Lid latch", answer="Cam latch",
        replace_same_topic=True,
    )
    second = decisions.record_answer(
        session, LEAD, subsystem="Enclosure", topic="gasket GAP", answer="0.050 in",
        replace_same_topic=True,
    )
    session.commit()
    session.expire_all()

    assert session.get(Decision, first.id).status == "superseded"
    assert session.get(Decision, first.id).superseded_by_id == second.id
    assert session.get(Decision, other.id).status == "confirmed"
    assert session.get(Decision, second.id).status == "confirmed"


def test_the_status_words_are_suggested_recorded_replaced():
    from models import DECISION_STATUS_LABELS

    assert DECISION_STATUS_LABELS == {
        "proposed": "Suggested",
        "confirmed": "Recorded",
        "superseded": "Replaced",
    }


# --- the Record page ---


def test_record_requires_sign_in(session):
    main.app.dependency_overrides.clear()
    with TestClient(main.app) as c:
        response = c.get("/record")

    assert response.status_code == 401
    assert "Sign in" in response.text


def test_record_lists_recorded_answers_by_part_and_date_newest_first(client, session):
    confirmed(session, topic="Latch style", confirmed_at=utc_now() - timedelta(days=3))
    confirmed(session, topic="Gasket gap", decision="0.050 in", confirmed_at=utc_now())
    confirmed(session, topic="Hole size", subsystem="Electronics Mounts", decision="M3")
    confirmed(session, topic="Thruster bolt", subsystem="Thrusters", decision="M4")
    make_decision(session, topic="Only suggested")
    make_decision(session, topic="Old answer", status="superseded")

    html = client.get("/record").text

    assert "<h1 class=\"page-title\">Team record</h1>" in html
    assert html.index("Gasket gap") < html.index("Latch style")  # newest first
    assert "Hole size" in html  # Electronics Mounts sit under Enclosure
    assert "Thruster bolt" not in html  # another part
    assert "Only suggested" not in html and "Old answer" not in html
    assert 'aria-current="true">Enclosure' in html
    assert "Recorded by lee@example.edu" in html
    # Work notes don't exist yet, so there is no toggle.
    assert "Show work notes" not in html


def test_record_shows_what_an_answer_replaces_and_where_it_came_from(client, session, task):
    old = make_decision(session, topic="Gasket gap", decision="0.030 in", status="superseded")
    new = confirmed(
        session, topic="Gasket gap", decision="0.050 in", rationale="From the 30 Mar notes.",
        confirmed_by="Lee Lead",
    )
    old.superseded_by_id = new.id
    session.add(old)
    session.commit()
    link_to(session, new, task)

    html = client.get("/record").text

    assert "Why: From the 30 Mar notes." in html
    assert "Replaces: 0.030 in." in html
    assert "Recorded by Lee Lead" in html and 'href="/tasks/M04"' in html
    assert f'id="answer-{new.id}"' in html
    assert f'href="{LINK}"' in html  # the source it was recorded from


def test_record_tabs_and_search(client, session):
    confirmed(session, topic="Thruster bolt", subsystem="Thrusters", decision="M4 bolts")
    confirmed(session, topic="Latch style")

    thrusters = client.get("/record?part=thrusters").text
    found = client.get("/record?part=enclosure&q=cam+latch").text
    none = client.get("/record?part=enclosure&q=zzz").text

    assert "Thruster bolt" in thrusters and "Latch style" not in thrusters
    assert "Latch style" in found
    assert "Nothing in Enclosure matches" in none


def test_an_empty_part_says_what_to_do(client, session):
    html = client.get("/record?part=seals").text

    assert "Nothing has been recorded for Seals yet" in html


def test_an_unknown_part_shows_a_notice_and_the_first_part(client, session):
    html = client.get("/record?part=wings").text

    assert "Unknown part &#39;wings&#39;" in html and "Enclosure" in html


def test_record_survives_a_database_failure(client, session, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(decisions, "recorded_entries", broken)

    response = client.get("/record")

    assert response.status_code == 503
    assert "Couldn&#39;t load the Record" in response.text and "secret" not in response.text
    assert 'href="/tasks"' in response.text  # the rest of the page still works


def test_team_record_says_what_it_holds_and_where_your_own_entries_are(client, session):
    html = client.get("/record").text

    assert "<title>Team record – Crush Coach</title>" in html
    assert (
        '<p class="page-sub">What the team has settled. Your own entries are in your Notebook.</p>'
        in html
    )
    assert "<span>Team record</span>" in html


def test_the_sidebar_says_record_not_decisions(client, session):
    html = client.get("/tasks").text

    assert 'href="/record"' in html and "<span>Team record</span>" in html
    assert "<span>Decisions</span>" not in html


def test_old_decision_links_land_on_the_entry_in_the_record(client, session):
    thruster = confirmed(session, topic="Thruster bolt", subsystem="Thrusters")
    redirect = client.get(f"/decisions/{thruster.id}", follow_redirects=False)

    assert redirect.headers["location"] == f"/record?part=thrusters#answer-{thruster.id}"
    assert client.get("/decisions/9999", follow_redirects=False).headers["location"] == "/record"
    assert client.get("/decisions", follow_redirects=False).headers["location"] == "/record"
    assert client.get("/decisions/new", follow_redirects=False).headers["location"] == "/record"


# --- on the task page ---


def link_to(session, decision, task):
    session.add(DecisionTask(decision_id=decision.id, task_id=task.id))
    session.commit()


def test_task_page_lists_recorded_answers_for_the_task(client, session, task):
    link_to(session, confirmed(session, topic="Latch style"), task)
    make_decision(session, topic="Unrelated thruster mount", subsystem="Thrusters")

    html = client.get("/tasks/M04").text

    assert "Recorded answers" in html
    assert "Latch style" in html and "Recorded" in html
    assert "Unrelated thruster mount" not in html


def test_task_page_without_recorded_answers_says_so(client, session, task):
    html = client.get("/tasks/M04").text

    assert "Nothing has been recorded from this task yet." in html


def test_task_page_no_longer_proposes_or_drafts_decisions(client, session, task):
    html = client.get("/tasks/M04").text

    assert "Propose a decision" not in html and "Draft a decision" not in html
    assert "decision-draft" not in html and "/decisions/new" not in html


def test_decision_failure_only_breaks_the_decisions_section(
    client, session, task, monkeypatch
):
    def broken(session, task_id):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(decisions, "decisions_for_task", broken)

    response = client.get("/tasks/M04")

    assert response.status_code == 200
    assert "Couldn&#39;t load this task&#39;s decisions" in response.text
    assert "secret" not in response.text
    assert "Finish enclosure wall panels" in response.text
    assert 'for="coach-message"' in response.text  # the coach still works


# --- the coach and decisions ---

HTMX = {"HX-Request": "true"}
DRIVE_LINK = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"


@pytest.fixture
def no_real_services(monkeypatch):
    """Anything that would reach the model or Drive fails the test instead."""
    monkeypatch.delenv("COACH_DAILY_LIMIT", raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("tried to use a real service")

    monkeypatch.setattr(llm, "get_client", refuse)
    monkeypatch.setattr(drive, "search", refuse)
    monkeypatch.setattr(drive, "read_text", refuse)


def script(monkeypatch, *items):
    model = fake.FakeClient(items)
    monkeypatch.setattr(llm, "get_client", lambda: model)
    return model


def ask(client, text="Which latch did we choose?"):
    return client.post("/tasks/M04/coach", data={"message": text}, headers=HTMX)


def search_confirmed(session, **kwargs):
    return decisions.search_confirmed(session, **kwargs)


def test_coach_search_returns_only_confirmed_decisions(session):
    confirmed(session, topic="Latch style")
    make_decision(session, topic="Latch style proposal")
    make_decision(session, topic="Latch style old", status="superseded")

    found = search_confirmed(session, query="latch")

    assert [d["topic"] for d in found] == ["Latch style"]
    assert found[0]["link"].startswith("/decisions/")
    assert found[0]["alternatives_rejected"][0]["option"] == "Toggle latch"
    assert "confirmed_by" not in found[0]  # members' emails aren't sent to the model


def test_coach_search_matches_keywords_filters_by_subsystem_and_ranks_the_task_first(
    session, task
):
    other = confirmed(session, topic="Latch style")
    linked = confirmed(session, topic="Panel material", decision="6061 aluminum.")
    confirmed(session, topic="Thruster mount", subsystem="Thrusters", decision="Bolt it.")
    link_to(session, linked, task)

    assert [d["topic"] for d in search_confirmed(session, query="latch")] == [
        "Latch style"
    ]
    assert search_confirmed(session, query="nothing matches this") == []
    assert [d["topic"] for d in search_confirmed(session, subsystem="Thrusters")] == [
        "Thruster mount"
    ]
    # An empty query lists confirmed decisions, this task's first.
    everything = search_confirmed(session, task_id=task.id)
    assert everything[0]["id"] == linked.id and everything[0]["related_to_this_task"]
    assert {d["id"] for d in everything} >= {linked.id, other.id}
    ranked = search_confirmed(session, query="latch aluminum", task_id=task.id)
    assert ranked[0]["id"] == linked.id


def test_linkify_makes_decision_paths_clickable_but_nothing_else(client):
    html = str(coach.linkify("See /decisions/12, not /admin/import or //evil.test/decisions/3."))

    assert '<a href="/decisions/12">/decisions/12</a>' in html
    assert "<a" not in html.replace('<a href="/decisions/12">/decisions/12</a>', "")


def test_the_coach_is_given_confirmed_decisions_first_and_links_both_on_a_conflict(
    monkeypatch, session, task, client, no_real_services
):
    decision = confirmed(session, topic="Latch style")
    link_to(session, decision, task)
    make_decision(session, topic="Latch style proposal: toggles")  # not binding
    add_file(
        session,
        "Latch design notes",
        folder="Alfie / Enclosure",
        modified="2023-01-02",
        chunks=["Use a toggle latch on the lid."],
        drive_id="1AbCdEfGhIjKlMnOp",
        link=DRIVE_LINK,
    )
    model = script(
        monkeypatch,
        fake.reply(
            f"Decision /decisions/{decision.id} says cam latch, but {DRIVE_LINK} "
            "says toggle. That document is possibly stale."
        ),
    )

    response = ask(client)

    assert response.status_code == 200
    html = response.text
    assert (
        f'<a href="/decisions/{decision.id}"' in html
    )  # the decision page is clickable
    assert f'<a href="{DRIVE_LINK}"' in html
    assert "Record: Latch style" in html  # listed under sources
    # One call; the confirmed decision (not the proposal) came with it, before Drive.
    assert len(model.requests) == 1
    sent = model.requests[0]["messages"][-1]["content"]
    assert "Use the two-point cam latch." in sent and "proposal" not in sent
    assert sent.index("Confirmed decisions") < sent.index("Drive sources")
    assert "they outrank Drive text" in sent
    assert {c.status for c in stored(session, LlmCall)} == {"ok"}


def test_a_decision_database_failure_is_noted_and_the_answer_still_arrives(
    monkeypatch, session, task, client, no_real_services
):
    add_file(session, "Latch notes", chunks=["latch"], drive_id="1AbCdEfGhIjKlMnOp")

    def broken(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(decisions, "search_confirmed", broken)
    script(monkeypatch, fake.reply("I couldn't check the decisions, so ask your lead."))

    response = ask(client)

    assert response.status_code == 200
    assert "ask your lead" in response.text
    assert "decision records couldn&#39;t be checked" in response.text
    assert "secret" not in response.text
    assert len(stored(session, LlmCall)) == 1  # the exchange was still logged


