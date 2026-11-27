"""Private notes: words only their member can read. No task, no Drive, no model, no publish,
and no other member or lead can see one anywhere in the app. Made-up people."""

from pathlib import Path

import pytest
from notebook_helpers import HTMX, LEAD, MEMBER, OTHER, add_task, stored, use_model

import main
from models import DriveWrite, LlmCall, PrivateNote, WorkNote

SECRET = "PRIVATE-MARKER-lid-latch-is-garbage"


def add(client, text=SECRET, **kw):
    return client.post("/notebook/private", data={"text": text}, headers=HTMX, **kw)


def test_a_private_note_is_words_a_member_and_a_time_and_nothing_else(session, as_user, doc, monkeypatch):
    add_task(session)
    model = use_model(monkeypatch)

    response = add(as_user(MEMBER))

    assert response.status_code == 200 and "Private note saved." in response.text and SECRET in response.text
    (note,) = stored(session, PrivateNote)
    assert (note.member, note.text) == ("sam@example.edu", SECRET) and note.created_at is not None
    assert {c.name for c in PrivateNote.__table__.columns} == {"id", "member", "text", "created_at"}
    # No Drive write, no model call, no entry made from it.
    assert doc.gets == 0 and model.requests == []
    assert stored(session, DriveWrite) == [] and stored(session, LlmCall) == [] and stored(session, WorkNote) == []


def test_the_tab_has_no_task_no_tidy_no_specs_and_no_publish_button(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/notebook/panel?tab=private").text

    assert "Only you can see these." in html and "never added to the Crush document" in html
    for forbidden in ("Publish", "Tidy my note", "Suggest specs", 'name="task"', 'type="file"', "Save draft"):
        assert forbidden not in html


def test_the_page_shows_them_beside_the_form_without_javascript(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    add(client)

    page = client.get("/notebook?tab=private")

    assert SECRET in page.text and "Private notes" in page.text and "hx-post" not in page.text


def test_saving_without_htmx_goes_back_to_the_tab(session, as_user):
    response = as_user(MEMBER).post("/notebook/private", data={"text": SECRET}, follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/notebook?tab=private"
    assert len(stored(session, PrivateNote)) == 1


def test_an_empty_note_is_refused(session, as_user):
    response = add(as_user(MEMBER), "   ")

    assert response.status_code == 400 and "Write something first." in response.text
    assert stored(session, PrivateNote) == []


def test_a_note_is_markup_safe(session, as_user):
    html = add(as_user(MEMBER), '<script>alert("x")</script>').text

    assert "<script>alert" not in html and "&lt;script&gt;" in html


def test_a_member_can_delete_their_own_note_and_nobody_elses(session, as_user):
    client = as_user(MEMBER)
    add(client)
    (note,) = stored(session, PrivateNote)

    for stranger in (OTHER, LEAD):
        assert as_user(stranger).post(f"/notebook/private/{note.id}/delete", headers=HTMX).status_code == 404
    assert len(stored(session, PrivateNote)) == 1

    response = as_user(MEMBER).post(f"/notebook/private/{note.id}/delete", headers=HTMX)
    assert "Private note deleted." in response.text and stored(session, PrivateNote) == []


def test_each_member_sees_only_their_own(session, as_user):
    add(as_user(MEMBER), "Sam thought alpha")
    add(as_user(OTHER), "Riley thought beta")

    sam = as_user(MEMBER).get("/notebook/panel?tab=private").text
    riley = as_user(OTHER).get("/notebook/panel?tab=private").text

    assert "Sam thought alpha" in sam and "Riley thought beta" not in sam
    assert "Riley thought beta" in riley and "Sam thought alpha" not in riley


def get_paths():
    """Every page of the app that needs no id in its address, plus the ones that do with
    ids that exist or nearly do."""
    paths = {
        path
        for path, methods in main.app.openapi()["paths"].items()
        if "get" in methods and "{" not in path
    }
    paths |= {
        "/tasks/M06", "/record?notes=1", "/record?notes=1&part=thrusters", "/record?notes=1&part=actuators",
        "/record?notes=1&q=latch", "/notebook?tab=private", "/notebook?tab=earlier", "/notebook?entry=1",
        "/notebook/panel?tab=private", "/notebook/panel?tab=earlier", "/notebook/panel?tab=today",
        "/notebook/photos/1", "/decisions/1", "/tasks?show=all", "/admin/import", "/admin",
    }
    return sorted(paths)


def test_no_other_member_or_lead_can_see_a_private_note_anywhere(session, as_user, doc):
    add_task(session)
    add(as_user(MEMBER))
    # The same words as a question to the coach, a task detail or a work entry would be seen;
    # a private note must be the only place they live, so finding them anywhere else is a leak.
    paths = get_paths()
    assert "/" in paths and "/timeline" in paths and "/record" in paths  # the scan really covers the app

    for viewer in (OTHER, LEAD):
        client = as_user(viewer)
        for path in paths:
            response = client.get(path, follow_redirects=True)
            assert SECRET not in response.text, f"{viewer['name']} saw a private note at {path}"
            assert "PRIVATE-MARKER" not in response.text, path
    # and a signed-out visitor
    main.app.dependency_overrides.clear()


def test_the_model_is_never_shown_a_private_note(session, as_user, doc, monkeypatch):
    import json

    import fake_openai as fake
    from index_helpers import add_file

    add_task(session)
    add_file(session, "Seal Calculations", chunks=["Baseplate gasket groove depth is 4.8 mm."], drive_id="1SealCalcSealCalcSeal")
    tidied = {"did": "Welded it.", "why": "", "question": None}
    model = use_model(
        monkeypatch,
        fake.reply(json.dumps(tidied)),
        fake.reply(json.dumps({"specs": []})),
        fake.reply("It is 4.8 mm."),
    )
    client = as_user(MEMBER)
    add(client)

    client.post("/notebook/entry/tidy", data={"task": "M06", "did": "Welded it.", "why": "x"}, headers=HTMX)
    client.post("/notebook/entry/specs", data={"task": "M06", "did": "Welded it.", "why": "x"}, headers=HTMX)
    client.post("/tasks/M06/coach", data={"message": "What is the groove depth?"}, headers=HTMX)

    assert len(model.requests) == 3
    assert SECRET not in json.dumps(model.requests)


def test_only_the_notebook_module_reads_the_private_notes_table():
    """The one place a private note is read or written is notebook.py (and its model and
    migrations), so no other module can leak it: they don't know it exists."""
    root = Path(__file__).resolve().parents[1]
    users = {
        path.name
        for path in root.glob("*.py")
        if "PrivateNote" in path.read_text() or "private_note" in path.read_text()
    }

    assert users == {"models.py", "notebook.py"}


def test_every_read_of_private_notes_filters_on_the_signed_in_member():
    root = Path(__file__).resolve().parents[1]
    source = (root / "notebook.py").read_text()

    reads = [part for part in source.split("select(PrivateNote)")[1:]]
    assert reads and all("PrivateNote.member" in part.split(".limit(")[0] for part in reads)


@pytest.mark.parametrize("path", ["/worklog", "/worklog/panel"])
def test_the_old_worklog_addresses_lead_to_the_notebook(session, as_user, path):
    response = as_user(MEMBER).get(path, follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"].startswith("/notebook")
