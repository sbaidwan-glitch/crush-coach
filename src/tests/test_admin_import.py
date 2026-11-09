"""/admin/import: lead-only upload, preview (saves nothing), then Import."""

import base64
import html
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlmodel import select

import auth
import db
import main
from models import Task

REAL = (Path(__file__).parent / "fixtures" / "tracker_real_layout.csv").read_bytes()

MEMBER = {"name": "Test Member", "email": "member@example.edu", "role": "member"}
LEAD = {"name": "Test Lead", "email": "lead@example.edu", "role": "lead"}


def make_client(user):
    if user is not None:
        main.app.dependency_overrides[auth.require_user] = lambda: user
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture
def lead():
    with make_client(LEAD) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def member():
    with make_client(MEMBER) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def anonymous():
    with make_client(None) as c:
        yield c


def upload(client, data=REAL, name="tracker.csv"):
    return client.post(
        "/admin/import/preview", files={"file": (name, data, "text/csv")}
    )


def rows_json(page: str) -> str:
    match = re.search(r'name="rows_json" value="([^"]*)"', page)
    assert match, "preview page has no rows_json field"
    return html.unescape(match.group(1))


def confirm(client, preview_html: str, **choices):
    return client.post(
        "/admin/import/confirm", data={"rows_json": rows_json(preview_html), **choices}
    )


def tasks(session) -> dict[str, Task]:
    session.expire_all()
    return {t.code: t for t in session.exec(select(Task)).all()}


# --- who may use it ---


def test_anonymous_is_sent_to_sign_in(anonymous):
    response = anonymous.get("/admin/import")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/admin/import"),
        ("post", "/admin/import/preview"),
        ("post", "/admin/import/confirm"),
    ],
)
def test_member_gets_403_everywhere(member, session, method, path):
    kwargs = {"files": {"file": ("t.csv", REAL, "text/csv")}} if "preview" in path else {}
    response = getattr(member, method)(path, **kwargs)

    assert response.status_code == 403
    assert "Leads only" in response.text
    assert tasks(session) == {}


def test_nav_link_is_only_for_leads(session):
    main.app.dependency_overrides[auth.require_user] = lambda: LEAD
    with make_client(LEAD) as client:
        assert "/admin/import" in client.get("/tasks").text
    main.app.dependency_overrides[auth.require_user] = lambda: MEMBER
    with make_client(MEMBER) as client:
        assert "/admin/import" not in client.get("/tasks").text
    main.app.dependency_overrides.clear()


def signed_cookie(email: str, role: str) -> str:
    """The cookie the OAuth callback would set, signed with the test secret."""
    payload = {"user": {"name": "Someone", "email": email, "role": role}}
    data = base64.b64encode(json.dumps(payload).encode())
    return TimestampSigner("test-only-session-secret").sign(data).decode()


@pytest.mark.parametrize(
    ("email", "cookie_role", "status"),
    [
        ("member@example.edu", "lead", 403),  # a forged "lead" role is ignored
        ("lead@example.edu", "member", 200),  # LEAD_EMAILS decides, not the cookie
    ],
)
def test_lead_role_comes_from_lead_emails(monkeypatch, email, cookie_role, status):
    """No dependency override: the real session and LEAD_EMAILS decide."""
    monkeypatch.setenv("ALLOWED_EMAILS", "lead@example.edu,member@example.edu")
    monkeypatch.setenv("LEAD_EMAILS", "lead@example.edu")
    with TestClient(main.app, follow_redirects=False) as client:
        client.cookies.set("crush_coach_session", signed_cookie(email, cookie_role))
        assert client.get("/admin/import").status_code == status


# --- upload and preview ---


def test_lead_sees_the_upload_form(lead):
    response = lead.get("/admin/import")

    assert response.status_code == 200
    assert 'enctype="multipart/form-data"' in response.text
    assert "Nothing is saved until you click Import" in response.text


def test_preview_shows_rows_guesses_and_errors_and_saves_nothing(lead, session):
    response = upload(lead)

    assert response.status_code == 200
    page = response.text
    assert "Nothing has been saved yet" in page
    assert "Cut example wall panels" in page
    # Subsystem guess is preselected; an unguessable row starts on "Choose…".
    assert re.search(r'name="subsystem_0"[^>]*>.*?<option value="Enclosure" selected', page, re.DOTALL)
    assert "Choose…" in page
    # Problems are listed with line numbers.
    assert "duplicate code M22(2)" in page
    assert "code is empty" in page
    assert "title is empty" in page
    assert "5 ready to import" in page
    assert "2 need a subsystem" in page
    assert tasks(session) == {}


def test_preview_marks_existing_codes_as_skipped(lead, session):
    session.add(
        Task(code="M01", title="Kept", subsystem="Enclosure", task_type="Fabricate", stage="drawing")
    )
    session.commit()

    page = upload(lead).text

    assert "Already exists: will be skipped" in page
    assert "1 already in the tracker" in page
    assert 'name="subsystem_0"' not in page  # no dropdown for a row that is skipped


def test_preview_escapes_cell_text(lead):
    data = b"#,Task,Detailed Description\nM01,<script>alert(1)</script>,<b>x</b>\n"

    page = upload(lead, data).text

    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (b"\xff\xfe\x00junk", "UTF-8"),
        (b"a,b\n1,2\n", "header row"),
    ],
)
def test_unusable_file_shows_a_plain_error_and_the_form_again(lead, data, message):
    response = upload(lead, data)

    assert response.status_code == 400
    assert message in response.text
    assert 'type="file"' in response.text


def test_no_file_chosen_is_a_plain_error(lead):
    response = lead.post("/admin/import/preview")
    assert response.status_code == 400
    assert "Choose a CSV file first" in response.text

    response = lead.post("/admin/import/preview", files={"file": ("", b"", "text/csv")})
    assert response.status_code == 400
    assert "Choose a CSV file first" in response.text


def test_database_failure_on_preview_is_reported_plainly(lead, monkeypatch):
    class Boom:
        def exec(self, *_a, **_k):
            raise RuntimeError("postgresql://user:secret@host/db is down")

    main.app.dependency_overrides[db.get_session] = lambda: Boom()
    try:
        response = upload(lead)
    finally:
        main.app.dependency_overrides.pop(db.get_session, None)

    assert response.status_code == 503
    assert "Nothing was saved" in response.text
    assert "secret" not in response.text


# --- Import ---


def test_confirm_saves_rows_with_the_leads_corrections(lead, session):
    preview = upload(lead).text

    response = confirm(
        lead,
        preview,
        subsystem_4="Gripper",  # M05 had no guess
        task_type_4="Design",
        subsystem_0="Thrusters",  # M01 corrected from the Enclosure guess
    )

    assert response.status_code == 200
    assert "6 added, 0 skipped (already existed), 4 with errors" in response.text
    saved = tasks(session)
    assert set(saved) == {"M01", "M02", "M03", "M04", "M05", "E01"}
    assert saved["M05"].subsystem == "Gripper"
    assert saved["M05"].task_type == "Design"
    assert saved["M05"].stage == "requirements"  # first stage of Design
    assert saved["M01"].subsystem == "Thrusters"
    assert saved["M03"].task_type == "Validate"
    assert saved["M03"].stage == "test plan"
    assert saved["M04"].stage == "drawing"
    assert saved["M01"].status == "todo"


def test_confirm_stores_the_tracker_fields(lead, session):
    preview = upload(lead).text
    confirm(lead, preview, subsystem_4="Gripper")

    task = tasks(session)["M01"]

    assert task.owner == "Sam Example"
    assert task.description == "Cut two example panels.\nDeburr the edges."
    assert task.dependencies == "M05"
    assert task.exploratory_questions == "Which sheet stock?"
    assert task.skills == "Waterjet basics"
    assert task.requirements == "Panels flat to the example spec"
    assert task.people_to_ask == "Riley Sample"
    assert task.documentation == "https://docs.google.com/document/d/example/edit"
    assert task.due_date == "Week 3"


def test_unchosen_subsystem_is_reported_not_saved(lead, session):
    preview = upload(lead).text

    response = confirm(lead, preview)  # M05 still on "Choose…"

    assert "no subsystem chosen" in response.text
    assert "M05" not in tasks(session)
    assert "M01" in tasks(session)


def test_existing_tasks_are_skipped_and_never_overwritten(lead, session):
    session.add(
        Task(
            code="M01",
            title="Edited by a lead",
            subsystem="Gripper",
            task_type="Design",
            stage="CAD",
            owner="Jordan Demo",
            status="in_progress",
        )
    )
    session.commit()
    preview = upload(lead).text

    response = confirm(lead, preview, subsystem_4="Gripper")

    assert "5 added, 1 skipped (already existed), 4 with errors" in response.text
    kept = tasks(session)["M01"]
    assert (kept.title, kept.owner, kept.status, kept.stage) == (
        "Edited by a lead",
        "Jordan Demo",
        "in_progress",
        "CAD",
    )
    assert kept.description is None


def test_importing_the_same_file_twice_adds_nothing_the_second_time(lead, session):
    confirm(lead, upload(lead).text, subsystem_4="Gripper")

    response = confirm(lead, upload(lead).text, subsystem_4="Gripper")

    assert "0 added, 6 skipped (already existed), 4 with errors" in response.text
    assert len(tasks(session)) == 6


def test_only_the_first_of_two_duplicate_codes_is_imported(lead, session):
    preview = upload(lead).text
    index = next(
        i for i, r in enumerate(json.loads(rows_json(preview))) if r["code"] == "M22(2)"
    )

    response = confirm(lead, preview, **{f"subsystem_{index}": "Enclosure"})

    assert tasks(session)["M22(2)"].title == "First example duplicate"
    assert "duplicate code M22(2) in this file" in response.text


def test_tampered_choices_are_rejected_not_saved(lead, session):
    preview = upload(lead).text

    response = confirm(
        lead, preview, subsystem_0="Cockpit", task_type_1="Welding", subsystem_4="Gripper"
    )

    assert "unknown subsystem &#39;Cockpit&#39;" in response.text
    assert "unknown task type &#39;Welding&#39;" in response.text
    saved = tasks(session)
    assert "M01" not in saved and "M02" not in saved
    assert "M03" in saved


def test_tampered_row_data_cannot_skip_validation(lead, session):
    evil = json.dumps(
        [
            {"line": 2, "code": "X01", "title": "t", "subsystem": "Cockpit", "task_type": "Design"},
            {"line": 3, "code": "X02", "title": "t", "subsystem": "Gripper", "task_type": "Design", "status": "bogus"},
            "not even an object",
        ]
    )

    response = lead.post("/admin/import/confirm", data={"rows_json": evil})

    assert response.status_code == 400
    assert tasks(session) == {}


@pytest.mark.parametrize("payload", ["", "not json", '{"a": 1}'])
def test_missing_or_damaged_preview_data_is_a_plain_error(lead, session, payload):
    response = lead.post("/admin/import/confirm", data={"rows_json": payload})

    assert response.status_code == 400
    assert "Upload the file again" in response.text
    assert tasks(session) == {}


def test_database_failure_on_confirm_saves_nothing_and_says_so(lead, session):
    preview = upload(lead).text

    class Boom:
        def exec(self, *_a, **_k):
            raise RuntimeError("postgresql://user:secret@host/db is down")

        def rollback(self):
            pass

    main.app.dependency_overrides[db.get_session] = lambda: Boom()
    try:
        response = confirm(lead, preview)
    finally:
        main.app.dependency_overrides.pop(db.get_session, None)

    assert response.status_code == 503
    assert "Nothing was saved" in response.text
    assert "secret" not in response.text
    assert tasks(session) == {}


def test_sample_format_also_imports_through_the_page(lead, session):
    sample = (Path(__file__).parent.parent / "sample_data" / "tasks.csv").read_bytes()

    preview = upload(lead, sample)
    assert "10 ready to import" in preview.text
    response = confirm(lead, preview.text)

    assert "10 added" in response.text
    assert tasks(session)["M02"].owner == "Sam Example"
