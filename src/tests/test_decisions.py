"""Decision records: pages, who may confirm or supersede, and validation. Made-up data only."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import auth
import decisions
import main
from models import Decision, DecisionTask, Task, utc_now

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
