"""/admin/facts: leads add, edit and remove the short facts every answer starts from."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import auth
import facts
import main
from models import TeamFact

MEMBER = {"name": "Test Member", "email": "member@example.edu", "role": "member"}
LEAD = {"name": "Test Lead", "email": "lead@example.edu", "role": "lead"}


def make_client(user):
    if user is not None:
        main.app.dependency_overrides[auth.require_user] = lambda: user
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture
def lead(session):
    with make_client(LEAD) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def member(session):
    with make_client(MEMBER) as c:
        yield c
    main.app.dependency_overrides.clear()


def add_fact(session, text="Current season is 2026-27") -> TeamFact:
    row = TeamFact(text=text)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def rows(session):
    session.expire_all()
    return list(session.exec(select(TeamFact).order_by(TeamFact.id)).all())


# --- who may use it ---


def test_anonymous_is_sent_to_sign_in():
    with make_client(None) as client:
        for method, path in (
            ("get", "/admin/facts"),
            ("post", "/admin/facts"),
            ("post", "/admin/facts/1"),
            ("post", "/admin/facts/1/delete"),
        ):
            response = getattr(client, method)(path)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/login")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/admin/facts"),
        ("post", "/admin/facts"),
        ("post", "/admin/facts/{id}"),
        ("post", "/admin/facts/{id}/delete"),
    ],
)
def test_a_member_gets_403_and_nothing_changes(member, session, method, path):
    fact = add_fact(session)

    response = getattr(member, method)(
        path.format(id=fact.id),
        **({"data": {"text": "Hacked"}} if method == "post" else {}),
    )

    assert response.status_code == 403
    assert [r.text for r in rows(session)] == ["Current season is 2026-27"]


# --- the page ---


def test_the_page_lists_every_fact_in_an_editable_form(lead, session):
    one = add_fact(session, "Current season is 2026-27")
    two = add_fact(session, "Crush is the current vehicle")

    response = lead.get("/admin/facts")

    assert response.status_code == 200
    html = response.text
    assert html.count("<h1 ") == 1 and "Team facts" in html
    for fact in (one, two):
        assert f'action="/admin/facts/{fact.id}"' in html
        assert f'action="/admin/facts/{fact.id}/delete"' in html
        assert f'for="fact-{fact.id}">' in html
    assert ">Current season is 2026-27</textarea>" in html
    assert 'for="new-fact">' in html
    assert 'maxlength="300"' in html


def test_the_page_has_an_empty_state(lead, session):
    html = lead.get("/admin/facts").text

    assert "No facts yet" in html and 'action="/admin/facts"' in html


def test_facts_are_html_escaped(lead, session):
    add_fact(session, "<script>alert(1)</script>")

    html = lead.get("/admin/facts").text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_the_page_survives_a_database_failure(lead, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(facts, "select", boom)

    response = lead.get("/admin/facts")

    assert response.status_code == 503
    assert "Couldn&#39;t load the team facts" in response.text
    assert "secret" not in response.text
    assert '<nav class="sidebar" aria-label="Main">' in response.text


# --- add, edit, remove ---


def test_adding_a_fact_saves_it_and_says_so(lead, session):
    response = lead.post("/admin/facts", data={"text": "  Crush   is the vehicle \n"})

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/facts?done=added"
    [row] = rows(session)
    assert row.text == "Crush is the vehicle"  # one tidy line
    assert row.updated_by == "lead@example.edu"
    assert "Fact added" in lead.get(response.headers["location"]).text


def test_editing_a_fact_changes_only_that_fact(lead, session):
    one = add_fact(session, "One")
    two = add_fact(session, "Two")

    response = lead.post(f"/admin/facts/{two.id}", data={"text": "Two, corrected"})

    assert response.headers["location"] == "/admin/facts?done=saved"
    assert [r.text for r in rows(session)] == ["One", "Two, corrected"]
    assert rows(session)[1].updated_by == "lead@example.edu"
    assert one.id != two.id


def test_removing_a_fact(lead, session):
    one = add_fact(session, "One")
    add_fact(session, "Two")

    response = lead.post(f"/admin/facts/{one.id}/delete")

    assert response.headers["location"] == "/admin/facts?done=removed"
    assert [r.text for r in rows(session)] == ["Two"]


def test_editing_or_removing_a_fact_that_is_gone_says_so(lead, session):
    for path in ("/admin/facts/999", "/admin/facts/999/delete"):
        response = lead.post(path, data={"text": "x"})

        assert response.headers["location"] == "/admin/facts?done=missing"
    assert "already gone" in lead.get("/admin/facts?done=missing").text


def test_the_result_line_comes_from_a_fixed_list_not_the_url(lead, session):
    html = lead.get("/admin/facts?done=<script>alert(1)</script>").text

    assert "<script>alert(1)</script>" not in html


@pytest.mark.parametrize(
    ("text", "message"),
    [("", "can't be empty"), ("   ", "can't be empty"), ("x" * 301, "300 characters")],
)
def test_a_bad_fact_is_refused_with_a_plain_message_and_keeps_the_text(
    lead, session, text, message
):
    existing = add_fact(session, "Keep me")

    added = lead.post("/admin/facts", data={"text": text})
    edited = lead.post(f"/admin/facts/{existing.id}", data={"text": text})

    for response in (added, edited):
        assert response.status_code == 400
        assert message in response.text.replace("&#39;", "'")
        assert 'role="alert"' in response.text
    assert [r.text for r in rows(session)] == ["Keep me"]
    if text.strip():
        assert text in added.text  # the too-long text is not lost


def test_there_is_a_limit_on_how_many_facts_go_into_every_prompt(
    lead, session, monkeypatch
):
    monkeypatch.setattr(facts, "MAX_FACTS", 2)
    add_fact(session, "One")
    add_fact(session, "Two")

    response = lead.post("/admin/facts", data={"text": "Three"})

    assert response.status_code == 400 and "already 2 facts" in response.text
    assert len(rows(session)) == 2


def test_a_database_failure_on_save_says_so_and_saves_nothing(
    lead, session, monkeypatch
):
    original = TeamFact

    def boom(**kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(facts, "TeamFact", boom)

    response = lead.post("/admin/facts", data={"text": "A fact"})

    assert response.status_code == 503
    assert "Nothing was saved" in response.text and "secret" not in response.text
    assert rows(session) == [] and original is TeamFact


def test_fact_texts_lists_them_oldest_first(session):
    add_fact(session, "First")
    add_fact(session, "Second")

    assert facts.fact_texts(session) == ["First", "Second"]
