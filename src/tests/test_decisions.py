"""Decision records: pages, who may confirm or supersede, and validation. Made-up data only."""

import json

import fake_openai as fake
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import auth
import coach
import decisions
import drive
import llm
import main
from models import CoachMessage, Decision, DecisionTask, LlmCall, Task, utc_now

LINK = "https://docs.google.com/document/d/1ExampleLatchNotes0001/edit"
MEMBER = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}
LEAD = {"name": "Lee Lead", "email": "lee@example.edu", "role": "lead"}


def sign_in_as(user):
    main.app.dependency_overrides[auth.require_user] = lambda: user


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
    return make_decision(
        session,
        status="confirmed",
        confirmed_by="lee@example.edu",
        confirmed_at=utc_now(),
        **overrides,
    )


def form_data(**overrides):
    data = {
        "subsystem": "Enclosure",
        "topic": "Panel material",
        "decision": "Use 6061 aluminum for the wall panels.",
        "rationale": "Cheap, easy to machine, and we have stock.",
        "alt_option_0": "Acrylic",
        "alt_why_0": "Cracks at the fastener holes.",
        "src_title_0": "Material notes",
        "src_link_0": LINK,
        "src_folder_0": "Crush / Enclosure",
        "src_year_0": "2026",
        "tasks": "",
    }
    data.update(overrides)
    return data


def stored(session, model):
    session.expire_all()
    return list(session.exec(select(model)).all())


# --- the list ---


def test_list_requires_sign_in(session):
    main.app.dependency_overrides.clear()
    with TestClient(main.app) as c:
        response = c.get("/decisions", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_empty_list_says_so_and_offers_to_propose(client, session):
    response = client.get("/decisions")

    assert response.status_code == 200
    assert "No decisions yet" in response.text
    assert 'href="/decisions/new"' in response.text


def test_list_shows_status_with_words_and_filters(client, session):
    confirmed(session, topic="Latch style")
    make_decision(session, topic="Gasket groove", subsystem="Seals/Waterproofing")

    everything = client.get("/decisions").text
    assert "Latch style" in everything and "Gasket groove" in everything
    assert "Confirmed" in everything and "Proposed" in everything

    by_subsystem = client.get("/decisions?subsystem=Seals/Waterproofing").text
    assert "Gasket groove" in by_subsystem and "Latch style" not in by_subsystem

    by_status = client.get("/decisions?status=confirmed").text
    assert "Latch style" in by_status and "Gasket groove" not in by_status

    none = client.get("/decisions?subsystem=Torpedo").text
    assert "No decisions match these filters" in none


def test_unknown_filter_values_show_a_notice_and_everything(client, session):
    make_decision(session, topic="Latch style")

    html = client.get("/decisions?subsystem=Wings&status=maybe").text

    assert "Unknown subsystem" in html and "Unknown status" in html
    assert "Latch style" in html


def test_list_survives_a_database_failure(client, session, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(type(session), "exec", broken)

    response = client.get("/decisions")

    assert response.status_code == 503
    assert "load decisions from the database" in response.text
    assert "secret" not in response.text
    assert 'href="/tasks"' in response.text  # the rest of the page still works


# --- the detail page ---


def test_detail_shows_everything_the_record_holds(client, session, task):
    decision = confirmed(session)
    session.add(DecisionTask(decision_id=decision.id, task_id=task.id))
    session.commit()

    html = client.get(f"/decisions/{decision.id}").text

    assert "Use the two-point cam latch." in html
    assert "Toggle latch" in html and "Loosens under vibration." in html
    assert "It seals evenly" in html
    assert f'href="{LINK}"' in html and "Crush / Enclosure" in html and "2026" in html
    assert "lee@example.edu" in html
    assert 'href="/tasks/M04"' in html


def test_missing_decision_is_a_plain_404(client, session):
    response = client.get("/decisions/999")

    assert response.status_code == 404
    assert "no decision" in response.text.lower()


def test_superseded_decision_links_to_what_replaced_it(client, session):
    new = confirmed(session, topic="Latch style (v2)")
    old = make_decision(
        session, status="superseded", topic="Latch style", superseded_by_id=new.id
    )

    old_html = client.get(f"/decisions/{old.id}").text
    new_html = client.get(f"/decisions/{new.id}").text

    assert f'href="/decisions/{new.id}"' in old_html and "Superseded" in old_html
    assert f'href="/decisions/{old.id}"' in new_html and "Supersedes" in new_html


# --- proposing ---


def test_member_can_propose_and_it_starts_proposed(client, session, task):
    response = client.post(
        "/decisions", data=form_data(tasks="m04"), follow_redirects=False
    )

    assert response.status_code == 303
    [saved] = stored(session, Decision)
    assert response.headers["location"] == f"/decisions/{saved.id}"
    assert saved.status == "proposed" and saved.proposed_by == "sam@example.edu"
    assert saved.confirmed_by is None and saved.confirmed_at is None
    assert saved.drafted_by_coach is False
    assert saved.alternatives == [
        {"option": "Acrylic", "rejected_because": "Cracks at the fastener holes."}
    ]
    assert saved.sources == [
        {"title": "Material notes", "link": LINK, "folder": "Crush / Enclosure", "year": 2026}
    ]
    [link] = stored(session, DecisionTask)
    assert (link.decision_id, link.task_id) == (saved.id, task.id)


def test_propose_form_prefills_from_a_task(client, session, task):
    html = client.get("/decisions/new?task=M04").text

    assert 'value="M04"' in html
    assert '<option value="Enclosure" selected>' in html


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"topic": ""}, "Topic is required"),
        ({"decision": ""}, "Decision is required"),
        ({"rationale": ""}, "Rationale is required"),
        ({"subsystem": "Wings"}, "Choose a subsystem"),
        ({"alt_why_0": ""}, "Alternative 1 needs both"),
        ({"src_link_0": "javascript:alert(1)"}, "link must start with https://docs.google.com/"),
        ({"src_link_0": "https://example.com/notes"}, "link must start with"),
        ({"src_title_0": ""}, "Source 1 needs a title and a Drive link"),
        ({"src_year_0": "26"}, "year must be four digits"),
        ({"tasks": "Z99"}, "No task has the code Z99"),
        ({"topic": "x" * 201}, "Topic is too long"),
    ],
)
def test_bad_proposals_are_refused_with_a_plain_message(
    client, session, task, overrides, message
):
    response = client.post("/decisions", data=form_data(**overrides))

    assert response.status_code == 400
    assert message in response.text
    assert "Nothing was saved" in response.text
    assert stored(session, Decision) == []
    # What the member typed is still in the form.
    assert "Use 6061 aluminum" in response.text or overrides.get("decision") == ""


def test_blank_alternative_and_source_rows_are_ignored(client, session):
    data = form_data(alt_option_1="", alt_why_1="", src_title_1="", src_link_1="")
    del data["alt_option_0"], data["alt_why_0"]
    del data["src_title_0"], data["src_link_0"], data["src_folder_0"], data["src_year_0"]

    response = client.post("/decisions", data=data, follow_redirects=False)

    assert response.status_code == 303
    [saved] = stored(session, Decision)
    assert saved.alternatives == [] and saved.sources == []


def test_propose_survives_a_database_failure(client, session, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(decisions, "create_decision", broken)

    response = client.post("/decisions", data=form_data())

    assert response.status_code == 503
    assert "Nothing was saved" in response.text
    assert "Use 6061 aluminum" in response.text  # the typing isn't lost


# --- who may confirm or supersede ---


def test_member_cannot_accept_or_supersede(client, session):
    proposal = make_decision(session)
    old = confirmed(session, topic="Old latch")

    accept = client.post(f"/decisions/{proposal.id}/accept")
    supersede = client.post(
        f"/decisions/{old.id}/supersede", data={"by": str(proposal.id)}
    )

    assert accept.status_code == 403 and supersede.status_code == 403
    session.expire_all()
    assert session.get(Decision, proposal.id).status == "proposed"
    assert session.get(Decision, old.id).status == "confirmed"


def test_member_does_not_see_lead_buttons(client, session):
    proposal = make_decision(session)

    html = client.get(f"/decisions/{proposal.id}").text

    assert "Accept decision" not in html
    assert "A lead accepts proposals" in html


def test_lead_accepts_a_proposal(lead_client, session):
    proposal = make_decision(session)

    page = lead_client.get(f"/decisions/{proposal.id}").text
    assert "Accept decision" in page

    response = lead_client.post(
        f"/decisions/{proposal.id}/accept", follow_redirects=False
    )

    assert response.status_code == 303
    session.expire_all()
    saved = session.get(Decision, proposal.id)
    assert saved.status == "confirmed"
    assert saved.confirmed_by == "lee@example.edu"
    assert saved.confirmed_at is not None


def test_accepting_with_replaces_supersedes_the_old_one(lead_client, session):
    old = confirmed(session, topic="Old latch")
    proposal = make_decision(session, topic="New latch")

    response = lead_client.post(
        f"/decisions/{proposal.id}/accept",
        data={"replaces": str(old.id)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    session.expire_all()
    assert session.get(Decision, proposal.id).status == "confirmed"
    old = session.get(Decision, old.id)
    assert old.status == "superseded" and old.superseded_by_id == proposal.id


def test_lead_supersedes_from_the_old_decision(lead_client, session):
    old = confirmed(session, topic="Old latch")
    proposal = make_decision(session, topic="New latch")

    page = lead_client.get(f"/decisions/{old.id}").text
    assert "Confirm replacement" in page and "New latch" in page

    response = lead_client.post(
        f"/decisions/{old.id}/supersede",
        data={"by": str(proposal.id)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    session.expire_all()
    assert session.get(Decision, old.id).superseded_by_id == proposal.id
    assert session.get(Decision, proposal.id).status == "confirmed"


def test_supersede_needs_a_choice(lead_client, session):
    old = confirmed(session)

    response = lead_client.post(f"/decisions/{old.id}/supersede", data={})

    assert response.status_code == 400
    assert "Choose the proposed decision" in response.text


def test_rules_are_enforced_on_the_server(lead_client, session):
    already = confirmed(session, topic="Already confirmed")
    other = confirmed(session, topic="Other confirmed")
    proposal = make_decision(session, topic="Proposal")
    gone = make_decision(session, topic="Gone", status="superseded")

    # Accepting something that isn't proposed.
    assert lead_client.post(f"/decisions/{already.id}/accept").status_code == 409
    # Superseding with something that is already confirmed.
    again = lead_client.post(
        f"/decisions/{already.id}/supersede", data={"by": str(other.id)}
    )
    assert again.status_code == 409
    # Replacing something that isn't confirmed.
    wrong = lead_client.post(
        f"/decisions/{proposal.id}/accept", data={"replaces": str(gone.id)}
    )
    assert wrong.status_code == 409 and "Only a confirmed decision" in wrong.text
    # A decision can't replace itself.
    own = lead_client.post(
        f"/decisions/{proposal.id}/accept", data={"replaces": str(proposal.id)}
    )
    assert own.status_code == 409
    # A missing decision.
    assert lead_client.post("/decisions/999/accept").status_code == 404

    session.expire_all()
    assert session.get(Decision, proposal.id).status == "proposed"  # nothing half-done
    assert session.get(Decision, other.id).superseded_by_id is None


def test_confirm_changes_nothing_if_the_save_fails(session, monkeypatch):
    old = confirmed(session, topic="Old latch")
    proposal = make_decision(session, topic="New latch")

    def broken():
        raise RuntimeError("down")

    monkeypatch.setattr(session, "commit", broken)
    with pytest.raises(RuntimeError):
        decisions.confirm(session, proposal, LEAD, replaces=old)

    monkeypatch.undo()
    session.expire_all()
    assert session.get(Decision, proposal.id).status == "proposed"
    assert session.get(Decision, old.id).status == "confirmed"
    assert session.get(Decision, old.id).superseded_by_id is None


def test_confirm_refuses_a_non_lead(session):
    proposal = make_decision(session)

    with pytest.raises(decisions.DecisionError):
        decisions.confirm(session, proposal, MEMBER)


def test_nav_links_to_decisions(client, session):
    assert 'href="/decisions"' in client.get("/tasks").text


# --- on the task page ---


def link_to(session, decision, task):
    session.add(DecisionTask(decision_id=decision.id, task_id=task.id))
    session.commit()


def test_task_page_lists_related_decisions_with_their_status(client, session, task):
    link_to(session, confirmed(session, topic="Latch style"), task)
    link_to(session, make_decision(session, topic="Gasket groove"), task)
    make_decision(session, topic="Unrelated thruster mount", subsystem="Thrusters")

    html = client.get("/tasks/M04").text

    assert "Latch style" in html and "Gasket groove" in html
    assert "Unrelated thruster mount" not in html
    assert "Confirmed" in html and "Proposed" in html
    assert html.index("Latch style") < html.index("Gasket groove")  # confirmed first
    assert 'href="/decisions/new?task=M04"' in html


def test_task_page_without_decisions_says_so(client, session, task):
    html = client.get("/tasks/M04").text

    assert "No decisions are linked to this task yet." in html
    assert 'href="/decisions/new?task=M04"' in html


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
    assert '<label for="coach-message">' in response.text  # the coach still works


def test_list_puts_confirmed_decisions_first(client, session):
    make_decision(session, topic="Gasket groove")
    confirmed(session, topic="Latch style")

    html = client.get("/decisions").text

    assert html.index("Latch style") < html.index("Gasket groove")


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


def draft(client):
    return client.post("/tasks/M04/decision-draft", headers=HTMX)


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


def test_coach_checks_decisions_before_drive_and_links_both_on_a_conflict(
    monkeypatch, session, task, client, no_real_services
):
    decision = confirmed(session, topic="Latch style")
    link_to(session, decision, task)
    make_decision(session, topic="Latch style proposal: toggles")  # not binding
    monkeypatch.setattr(
        drive,
        "search",
        lambda query, limit=10: [
            {
                "title": "Latch design notes",
                "link": DRIVE_LINK,
                "mime_type": "application/vnd.google-apps.document",
                "modified_time": "2023-01-02T00:00:00Z",
                "folder_path": "Alfie / Enclosure",
                "locations": [],
            }
        ],
    )
    model = script(
        monkeypatch,
        fake.tool_request(("c1", "drive_search", json.dumps({"query": "latch"}))),
        fake.tool_request(("c2", "find_decisions", json.dumps({"query": "latch"}))),
        fake.tool_request(("c3", "drive_search", json.dumps({"query": "latch"}))),
        fake.reply(
            f"Decision /decisions/{decision.id} says cam latch, but {DRIVE_LINK} "
            "says toggle. That document is possibly stale."
        ),
    )

    response = ask(client)

    assert response.status_code == 200
    html = response.text
    assert f'<a href="/decisions/{decision.id}"' in html  # the decision page is clickable
    assert f'<a href="{DRIVE_LINK}"' in html
    assert f"Decision D{decision.id}: Latch style" in html  # listed under sources
    # Drive was refused the first time, then the decision came back, without the proposal.
    first = model.requests[1]["messages"][-1]["content"]
    assert "Call find_decisions first" in first
    found = model.requests[2]["messages"][-1]["content"]
    assert "Use the two-point cam latch." in found and "proposal" not in found
    assert {c.status for c in stored(session, LlmCall)} == {"ok"}


def test_a_decision_database_failure_is_noted_and_the_answer_still_arrives(
    monkeypatch, session, task, client, no_real_services
):
    def broken(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(decisions, "search_confirmed", broken)
    script(
        monkeypatch,
        fake.tool_request(("c1", "find_decisions", json.dumps({"query": "latch"}))),
        fake.reply("I couldn't check the decisions, so ask your lead."),
    )

    response = ask(client)

    assert response.status_code == 200
    assert "ask your lead" in response.text
    assert "decision records couldn&#39;t be checked" in response.text
    assert "secret" not in response.text
    assert len(stored(session, LlmCall)) == 2  # the exchange was still logged


# --- the coach drafts a decision ---

DRAFT_JSON = {
    "subsystem": "Enclosure",
    "topic": "Latch style",
    "decision": "Use the two-point cam latch.",
    "rationale": "Toggles loosen under vibration.",
    "alternatives": [{"option": "Toggle", "rejected_because": "Loosens."}],
    "sources": [
        {"title": "Latch design notes", "link": DRIVE_LINK, "folder": "Crush", "year": 2026},
        {"title": "Invented", "link": "https://docs.google.com/document/d/nope1234567/edit"},
    ],
}


def seed_conversation(session, task):
    session.add_all(
        [
            CoachMessage(task_id=task.id, member="sam@example.edu", role="user",
                         content="Cam or toggle latch?"),
            CoachMessage(task_id=task.id, member="Coach", role="assistant",
                         content=f"The notes {DRIVE_LINK} favor a cam latch."),
        ]
    )
    session.commit()


def test_draft_returns_a_prefilled_form_and_saves_no_decision(
    monkeypatch, session, task, client, no_real_services
):
    seed_conversation(session, task)
    script(monkeypatch, fake.reply(json.dumps(DRAFT_JSON)))

    response = draft(client)

    assert response.status_code == 200
    html = response.text
    assert "Draft decision" in html and "not saved yet" in html
    assert 'value="Latch style"' in html and "Use the two-point cam latch." in html
    assert 'name="drafted" value="1"' in html
    assert DRIVE_LINK in html and "nope1234567" not in html  # invented link dropped
    assert 'value="M04"' in html and '<option value="Enclosure" selected>' in html
    assert stored(session, Decision) == []  # nothing is a decision until submitted
    [call] = stored(session, LlmCall)
    assert call.status == "ok" and call.member == "sam@example.edu"
    thread = [m.content for m in stored(session, CoachMessage)]
    assert "Draft a decision from this conversation." in thread
    assert any(c.startswith("Drafted a decision: Latch style") for c in thread)


def test_submitting_a_draft_makes_a_proposal_a_lead_can_accept(
    monkeypatch, session, task, client, no_real_services
):
    seed_conversation(session, task)
    script(monkeypatch, fake.reply(json.dumps(DRAFT_JSON)))
    html = draft(client).text
    assert 'action="/decisions"' in html

    data = form_data(
        topic="Latch style", decision="Use the two-point cam latch.", tasks="M04",
        drafted="1",
    )
    response = client.post("/decisions", data=data, follow_redirects=False)

    assert response.status_code == 303
    [saved] = stored(session, Decision)
    assert saved.status == "proposed" and saved.drafted_by_coach is True
    assert "(drafted by the coach)" in client.get(f"/decisions/{saved.id}").text

    sign_in_as(LEAD)
    client.post(f"/decisions/{saved.id}/accept")
    session.expire_all()
    assert session.get(Decision, saved.id).status == "confirmed"


def test_draft_needs_a_coach_answer_first(
    monkeypatch, session, task, client, no_real_services
):
    response = draft(client)  # the model would raise if called

    assert "nothing to draft from yet" in response.text
    assert stored(session, LlmCall) == [] and stored(session, CoachMessage) == []


def test_draft_counts_toward_the_daily_cap(
    monkeypatch, session, task, client, no_real_services
):
    monkeypatch.setenv("COACH_DAILY_LIMIT", "1")
    seed_conversation(session, task)  # sam's one message today is already used
    session.exec(select(CoachMessage))  # (the seeded user message is "today")

    response = draft(client)

    assert "used your 1 coach messages" in response.text
    assert stored(session, LlmCall) == []


def test_draft_failure_says_so_and_keeps_the_page_working(
    monkeypatch, session, task, client, no_real_services
):
    seed_conversation(session, task)
    script(monkeypatch, fake.timed_out())

    response = draft(client)

    assert response.status_code == 200
    assert "took too long" in response.text
    assert stored(session, Decision) == []
    [call] = stored(session, LlmCall)
    assert call.status == "timeout"
    assert client.get("/tasks/M04").status_code == 200


def test_draft_that_is_not_json_is_a_plain_error(
    monkeypatch, session, task, client, no_real_services
):
    seed_conversation(session, task)
    script(monkeypatch, fake.reply("You decided on the cam latch."))

    response = draft(client)

    assert "couldn&#39;t turn this conversation into a draft" in response.text
    assert stored(session, Decision) == []


def test_draft_without_javascript_shows_the_form_on_its_own_page(
    monkeypatch, session, task, client, no_real_services
):
    seed_conversation(session, task)
    script(monkeypatch, fake.reply(json.dumps(DRAFT_JSON)))

    response = client.post("/tasks/M04/decision-draft")  # no HX-Request header

    assert response.status_code == 200
    assert "<h1>Propose a decision</h1>" in response.text
    assert 'value="Latch style"' in response.text


def test_task_page_offers_the_draft_button(client, session, task):
    html = client.get("/tasks/M04").text

    assert 'hx-post="/tasks/M04/decision-draft"' in html
    assert "Draft a decision from this conversation" in html
    assert 'id="decision-draft"' in html
