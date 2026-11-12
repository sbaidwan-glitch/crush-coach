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


def confirm(client, preview_html: str, **fields):
    body = {"rows_json": rows_json(preview_html), "filename": "tracker.csv", **fields}
    return client.post("/admin/import/confirm", data=body)


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
        ("post", "/admin/import/remove/preview"),
        ("post", "/admin/import/remove/confirm"),
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


# --- upload and review ---

# Row numbers in tests/fixtures/tracker_real_layout.csv, as the form names them.
M01, M05, DUP_FIRST, DUP_SECOND, NO_CODE, NO_TITLE, E01 = 0, 4, 5, 6, 7, 8, 9


def fix_everything(**extra):
    """Form values that resolve every problem row in the fixture."""
    return {
        f"subsystem_{M05}": "Gripper",
        f"code_{DUP_SECOND}": "M25",
        f"code_{NO_CODE}": "M26",
        f"title_{NO_TITLE}": "Example title",
        f"subsystem_{NO_TITLE}": "Enclosure",
        **extra,
    }


def includes(count=10):
    return {f"include_{i}": "1" for i in range(count)}


def check(client, preview_html, **fields):
    """What the page posts when the lead edits something (all rows stay included)."""
    body = {"rows_json": rows_json(preview_html), "filename": "tracker.csv", **fields}
    return client.post("/admin/import/check", data=body)


def summary(page: str) -> str:
    match = re.search(r'id="review-summary"[^>]*>(.*?)</p>', page, re.DOTALL)
    assert match, "no summary on the page"
    return " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split())


def import_disabled(page: str) -> bool:
    button = re.search(r"<button[^>]*formaction=\"/admin/import/confirm\"[^>]*>", page)
    assert button, "no Import button"
    return " disabled" in button.group(0)


def test_lead_sees_the_upload_form(lead):
    response = lead.get("/admin/import")

    assert response.status_code == 200
    assert 'enctype="multipart/form-data"' in response.text
    assert "Nothing is saved until you click Import" in response.text
    assert "Remove tasks" in response.text


def test_review_shows_a_summary_and_saves_nothing(lead, session):
    response = upload(lead)

    assert response.status_code == 200
    assert "Nothing has been saved yet" in response.text
    assert summary(response.text) == "6 ready · 4 need a fix"
    assert import_disabled(response.text)
    assert "Import is off until 4 rows are fixed or left out." in response.text
    assert tasks(session) == {}


def test_every_row_keeps_a_visible_status(lead):
    page = upload(lead).text

    assert page.count("Will be added") == 6
    assert page.count("Needs a fix") == 4


def test_guessed_subsystems_are_preselected(lead):
    page = upload(lead).text

    assert re.search(r'name="subsystem_0".*?<option value="Enclosure" selected', page, re.DOTALL)
    assert re.search(r'name="subsystem_3".*?<option value="Thrusters" selected', page, re.DOTALL)


def test_duplicate_code_row_asks_for_a_new_code_with_the_next_free_one_prefilled(lead):
    page = upload(lead).text

    assert "Two rows use M22(2). Give this one a new code:" in page
    box = re.search(r'<input id="code-6"[^>]*>', page).group(0)
    assert 'value="M23"' in box  # M22(2) counts as M22, so M23 is the next free code
    assert 'aria-invalid="true"' in box
    assert 'aria-describedby="code-msg-6"' in box
    assert "Use this code" in page


def test_other_problems_show_a_short_message_under_their_own_field(lead):
    page = upload(lead).text

    assert "Choose a subsystem." in page
    assert "Add a title." in page
    assert "Add a code for this row." in page
    # The subsystem message sits right after the subsystem select, in the same cell.
    assert re.search(
        r'name="subsystem_4".*?</select>\s*<p class="field-error" id="subsystem-msg-4"',
        page,
        re.DOTALL,
    )


def test_existing_codes_with_nothing_to_change_are_shown_as_skipped_with_no_controls(
    lead, session
):
    session.add(
        Task(
            code="E01",
            title="Print example tray",
            subsystem="Electronics Mounts",
            task_type="Fabricate",
            stage="CAD",
            due_date="TBD",
        )
    )
    session.commit()

    page = upload(lead).text

    assert "Already in the tracker, no changes. Skipped." in page
    assert f'name="subsystem_{E01}"' not in page
    assert "1 already in the tracker" in summary(page)


def test_review_escapes_cell_text(lead):
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


class BrokenDatabase:
    def exec(self, *_a, **_k):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    def rollback(self):
        pass


@pytest.fixture
def broken_database():
    main.app.dependency_overrides[db.get_session] = lambda: BrokenDatabase()
    yield
    main.app.dependency_overrides.pop(db.get_session, None)


def test_database_failure_on_upload_is_reported_plainly(lead, broken_database):
    response = upload(lead)

    assert response.status_code == 503
    assert "Nothing was saved" in response.text
    assert "secret" not in response.text


# --- fixing rows in place ---


def test_using_the_suggested_code_fixes_the_row_and_updates_the_summary(lead):
    preview = upload(lead).text

    page = check(lead, preview, **includes(), **{f"code_{DUP_SECOND}": "M06"}).text

    assert "Two rows use" not in page
    assert "needs a fix" in summary(page) or "need a fix" in summary(page)
    assert summary(page) == "7 ready · 3 need a fix"
    # The fixed row keeps its code box with the new value, and says it will be added.
    assert re.search(r'<input id="code-6"[^>]*value="M06"', page)


def suggested(preview_html, index):
    return re.search(rf'name="suggested_{index}" value="([^"]*)"', preview_html).group(1)


def test_an_untouched_suggestion_is_not_applied_until_the_lead_accepts_it(lead):
    preview = upload(lead).text
    dup, blank = suggested(preview, DUP_SECOND), suggested(preview, NO_CODE)
    assert (dup, blank) == ("M23", "M24")

    # The page posts every box. Only the dup row's button was clicked.
    page = check(
        lead,
        preview,
        **includes(),
        **{
            f"code_{DUP_SECOND}": dup,
            f"suggested_{DUP_SECOND}": dup,
            f"code_{NO_CODE}": blank,
            f"suggested_{NO_CODE}": blank,
            "accept": str(DUP_SECOND),
        },
    ).text

    assert summary(page) == "7 ready · 3 need a fix"
    assert "Add a code for this row." in page  # still waiting on the lead
    assert "Two rows use" not in page


def test_typing_over_a_suggestion_applies_it_without_the_button(lead):
    preview = upload(lead).text
    dup = suggested(preview, DUP_SECOND)

    page = check(
        lead,
        preview,
        **includes(),
        **{f"code_{DUP_SECOND}": "M40", f"suggested_{DUP_SECOND}": dup},
    ).text

    assert "Two rows use" not in page
    assert summary(page) == "7 ready · 3 need a fix"


def test_a_typed_code_that_is_already_in_the_tracker_is_a_problem(lead, session):
    session.add(
        Task(code="M25", title="Taken", subsystem="Enclosure", task_type="Fabricate", stage="drawing")
    )
    session.commit()
    preview = upload(lead).text

    page = check(lead, preview, **includes(), **{f"code_{DUP_SECOND}": "M25"}).text

    assert "M25 is already in the tracker. Use a different code:" in page
    assert re.search(r'<input id="code-6"[^>]*value="M25"', page)  # the lead's value stays


def test_import_is_enabled_once_every_included_row_is_valid(lead):
    preview = upload(lead).text

    page = check(lead, preview, **includes(), **fix_everything()).text

    assert summary(page) == "10 ready · 0 need a fix"
    assert not import_disabled(page)
    assert "Import 10 tasks" in page
    assert "Import is off" not in page


def test_leaving_a_problem_row_out_also_clears_the_block(lead):
    preview = upload(lead).text
    rows = includes()
    for index in (M05, DUP_SECOND, NO_CODE, NO_TITLE):
        del rows[f"include_{index}"]

    page = check(lead, preview, **rows).text

    assert summary(page) == "6 ready · 0 need a fix · 4 left out"
    assert not import_disabled(page)
    assert page.count("Left out") >= 4


def test_nothing_to_import_keeps_the_button_off(lead):
    page = check(lead, upload(lead).text).text  # no include_ fields: everything left out

    assert import_disabled(page)
    assert "no rows to import yet" in page


def test_check_saves_nothing(lead, session):
    check(lead, upload(lead).text, **includes(), **fix_everything())

    assert tasks(session) == {}


def test_check_reports_damaged_preview_data(lead):
    response = lead.post("/admin/import/check", data={"rows_json": "nope"})

    assert response.status_code == 400
    assert "Upload the file again" in response.text


# --- Import ---


def test_confirm_refuses_while_an_included_row_needs_a_fix(lead, session):
    preview = upload(lead).text

    response = confirm(lead, preview, **includes())

    assert response.status_code == 400
    assert "Nothing was saved. Fix the rows marked below" in response.text
    assert import_disabled(response.text)
    assert tasks(session) == {}


def test_confirm_saves_the_fixed_rows_with_the_leads_corrections(lead, session):
    preview = upload(lead).text

    response = confirm(
        lead,
        preview,
        **includes(),
        **fix_everything(**{f"subsystem_{M01}": "Thrusters", f"task_type_{M05}": "Design"}),
    )

    assert response.status_code == 200
    assert "10 added, 0 updated, 0 skipped (already existed), 0 with errors" in response.text
    saved = tasks(session)
    assert set(saved) == {
        "M01", "M02", "M03", "M04", "M05", "M22(2)", "M25", "M26", "M07", "E01"
    }
    assert saved["M25"].title == "Make example claw jaw B"  # the duplicate, renamed
    assert saved["M26"].title.startswith("Orphan")
    assert saved["M07"].title == "Example title"
    assert saved["M05"].subsystem == "Gripper"
    assert saved["M05"].stage == "requirements"  # first stage of Design
    assert saved["M01"].subsystem == "Thrusters"
    assert saved["M03"].stage == "test plan"
    assert saved["M04"].stage == "drawing"


def test_confirm_stores_the_tracker_fields(lead, session):
    confirm(lead, upload(lead).text, **includes(), **fix_everything())

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


def test_rows_left_out_are_not_saved(lead, session):
    rows = includes()
    for index in (M05, DUP_SECOND, NO_CODE, NO_TITLE):
        del rows[f"include_{index}"]

    response = confirm(lead, upload(lead).text, **rows)

    assert "6 added" in response.text
    assert set(tasks(session)) == {"M01", "M02", "M03", "M04", "M22(2)", "E01"}


def existing_m01(**kw):
    return Task(
        **{
            "code": "M01",
            "title": "Edited by a lead",
            "subsystem": "Gripper",
            "task_type": "Design",
            "stage": "CAD",
            "owner": "Jordan Demo",
            "status": "in_progress",
            **kw,
        }
    )


def test_a_reimport_lists_what_changed_on_an_existing_task(lead, session):
    session.add(existing_m01())
    session.commit()

    page = upload(lead).text

    assert "1 to update" in summary(page)
    assert "Will be updated" in page
    assert "Title:" in page and "Edited by a lead" in page
    assert "Cut example wall panels" in page
    assert "Owner:" in page and "Jordan Demo" in page and "Sam Example" in page
    # A long, multi-line field opens to show the whole before and after.
    assert "Description changed" in page
    assert tasks(session)["M01"].title == "Edited by a lead"  # a preview saves nothing


def test_confirm_updates_the_changed_fields_and_leaves_progress_and_guesses_alone(
    lead, session
):
    session.add(existing_m01())
    session.commit()

    response = confirm(lead, upload(lead).text, **includes(), **fix_everything())

    assert "9 added, 1 updated, 0 skipped (already existed), 0 with errors" in response.text
    assert "Title, Owner, Description" in response.text
    kept = tasks(session)["M01"]
    assert (kept.title, kept.owner) == ("Cut example wall panels", "Sam Example")
    assert kept.description == "Cut two example panels.\nDeburr the edges."
    assert kept.due_date == "Week 3"
    # Progress is the app's, and subsystem/type in a tracker export are only guesses.
    assert (kept.status, kept.stage) == ("in_progress", "CAD")
    assert (kept.subsystem, kept.task_type) == ("Gripper", "Design")


def test_leaving_an_update_out_keeps_the_task_as_it_is(lead, session):
    session.add(existing_m01())
    session.commit()
    rows = includes()
    del rows[f"include_{M01}"]

    response = confirm(lead, upload(lead).text, **rows, **fix_everything())

    assert "9 added, 0 updated" in response.text
    assert tasks(session)["M01"].title == "Edited by a lead"


def test_a_cell_cleared_in_the_sheet_clears_the_field_but_not_the_title(lead, session):
    session.add(existing_m01(code="M02", title="Design example lid latch", owner="Old Owner"))
    session.commit()

    preview = upload(lead).text
    assert "(empty)" in preview
    response = confirm(lead, preview, **includes(), **fix_everything())

    assert "1 updated" in response.text
    assert tasks(session)["M02"].owner is None


def test_an_update_that_would_blank_the_title_needs_a_fix(lead, session):
    session.add(existing_m01(code="M07", title="Kept title", owner=None))
    session.commit()

    preview = upload(lead).text

    assert "Add a title." in preview
    response = confirm(lead, preview, **includes(), **fix_everything(**{f"title_{NO_TITLE}": ""}))
    assert response.status_code == 400
    assert tasks(session)["M07"].title == "Kept title"


def test_a_sample_reimport_updates_subsystem_type_and_owner_but_not_stage_or_status(
    lead, session
):
    session.add(
        Task(
            code="X01",
            title="Example",
            subsystem="Gripper",
            task_type="Design",
            stage="CAD",
            status="done",
            owner="Old",
            description="Kept: the sample has no such column",
        )
    )
    session.commit()
    data = (
        b"code,title,subsystem,task_type,stage,owner,status\n"
        b"X01,Example,Torpedo,Validate,test plan,New,todo\n"
    )

    preview = upload(lead, data).text
    assert "Subsystem:" in preview and "Type:" in preview and "Owner:" in preview
    assert "Stage:" not in preview and "Status:" not in preview
    confirm(lead, preview, include_0="1", is_tracker="0")

    task = tasks(session)["X01"]
    assert (task.subsystem, task.task_type, task.owner) == ("Torpedo", "Validate", "New")
    assert (task.stage, task.status) == ("CAD", "done")
    assert task.description == "Kept: the sample has no such column"


def test_a_form_without_the_layout_flag_updates_only_the_tracker_fields(lead, session):
    """A missing is_tracker must not make a tracker export overwrite subsystem or type."""
    session.add(existing_m01(code="X01", title="Example"))
    session.commit()
    data = (
        b"code,title,subsystem,task_type,stage,owner,status\n"
        b"X01,Example,Torpedo,Validate,,New,\n"
    )

    confirm(lead, upload(lead, data).text, include_0="1")  # no is_tracker field

    task = tasks(session)["X01"]
    assert task.owner == "New"
    assert (task.subsystem, task.task_type) == ("Gripper", "Design")


def test_update_text_from_the_file_is_escaped(lead, session):
    session.add(existing_m01(code="M01", title="<i>old</i>"))
    session.commit()
    data = b"#,Task\nM01,<script>alert(1)</script>\n"

    page = upload(lead, data).text

    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page and "&lt;i&gt;old" in page


def test_importing_the_same_file_twice_adds_nothing_the_second_time(lead, session):
    confirm(lead, upload(lead).text, **includes(), **fix_everything())

    page = upload(lead).text
    assert "already in the tracker" in summary(page)

    response = confirm(lead, page, **includes(), **fix_everything())

    # M25 and M26 were typed in as new codes but now exist: that is a problem, not a skip.
    assert response.status_code == 400
    assert len(tasks(session)) == 10


def test_tampered_choices_are_rejected_not_saved(lead, session):
    preview = upload(lead).text

    response = confirm(
        lead,
        preview,
        **includes(),
        **fix_everything(**{f"subsystem_{M01}": "Cockpit", f"task_type_{M01 + 1}": "Welding"}),
    )

    assert response.status_code == 400
    assert "“Cockpit” isn’t a subsystem. Choose one." in response.text
    assert "Choose a type." in response.text
    assert tasks(session) == {}


def test_tampered_row_data_cannot_skip_validation(lead, session):
    evil = json.dumps(
        [
            {"line": 2, "code": "X01", "title": "t", "subsystem": "Cockpit", "task_type": "Design"},
            {"line": 3, "code": "X02", "title": "t", "subsystem": "Gripper", "task_type": "Design", "status": "bogus"},
        ]
    )

    response = lead.post(
        "/admin/import/confirm", data={"rows_json": evil, "include_0": "1", "include_1": "1"}
    )

    assert response.status_code == 400
    assert tasks(session) == {}


@pytest.mark.parametrize("payload", ["", "not json", '{"a": 1}', '["not an object"]'])
def test_missing_or_damaged_preview_data_is_a_plain_error(lead, session, payload):
    response = lead.post("/admin/import/confirm", data={"rows_json": payload})

    assert response.status_code == 400
    assert "Upload the file again" in response.text
    assert tasks(session) == {}


def test_database_failure_on_confirm_saves_nothing_and_says_so(lead, session):
    preview = upload(lead).text
    main.app.dependency_overrides[db.get_session] = lambda: BrokenDatabase()
    try:
        response = confirm(lead, preview, **includes(), **fix_everything())
    finally:
        main.app.dependency_overrides.pop(db.get_session, None)

    assert response.status_code == 503
    assert "Nothing was saved" in response.text
    assert "secret" not in response.text
    assert tasks(session) == {}


def test_sample_format_also_imports_through_the_page(lead, session):
    sample = (Path(__file__).parent.parent / "sample_data" / "tasks.csv").read_bytes()

    preview = upload(lead, sample)
    assert summary(preview.text) == "10 ready · 0 need a fix"
    assert not import_disabled(preview.text)
    response = confirm(lead, preview.text, **includes())

    assert "10 added" in response.text
    assert tasks(session)["M02"].owner == "Sam Example"


def test_sample_row_with_an_unknown_subsystem_is_fixable_in_the_review(lead, session):
    data = (
        b"code,title,subsystem,task_type,stage,owner,status\n"
        b"X01,Example,Cockpit,Design,requirements,,\n"
    )

    preview = upload(lead, data).text

    assert "“Cockpit” isn’t a subsystem. Choose one." in preview
    assert import_disabled(preview)
    response = confirm(lead, preview, include_0="1", subsystem_0="Gripper")
    assert "1 added" in response.text
    assert tasks(session)["X01"].subsystem == "Gripper"


def test_pressing_enter_in_a_field_checks_and_never_imports(lead):
    page = check(lead, upload(lead).text, **includes(), **fix_everything()).text

    first_submit = re.search(r'<button[^>]*type="submit"[^>]*>', page).group(0)

    assert "formaction" not in first_submit  # the form's default button re-checks
    assert not import_disabled(page)
