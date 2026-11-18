"""Writing to the Crush document: where an entry goes, that it only ever adds text, that it
happens once, and what a failed write leaves behind. Uses a fake Docs client and made-up
data; no real document id appears anywhere."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fake_crush_doc import DOC_ID, FakeCrushDoc
from fake_google import http_error
from sqlmodel import select

import drive
import drive_write
from models import Decision, DecisionTask, DriveWrite, Task, WorkNote, utc_now


def at(day: int, hour: int, month: int = 11) -> datetime:
    """A stored (naive UTC) moment in 2026."""
    return datetime(2026, month, day, hour, 0, tzinfo=UTC).replace(tzinfo=None)


DAY = at(17, 20)  # 12:00 in Los Angeles: 17 Nov 2026
HEADING = "17 Nov 2026"


@pytest.fixture(autouse=True)
def crush_doc_env(monkeypatch):
    monkeypatch.setenv("DECISIONS_DOC_ID", DOC_ID)
    monkeypatch.setenv("COACH_TIMEZONE", "America/Los_Angeles")

    def no_network():
        raise AssertionError("a test reached for the real Google Docs client")

    monkeypatch.setattr(drive_write, "build_docs", no_network)


@pytest.fixture
def doc():
    return FakeCrushDoc("Enclosure", "Seals", "Thrusters", "Actuators", "General")


def make_note(session, did="Tack-welded the four walls.", why="", subsystem="Enclosure", **kw):
    note = WorkNote(
        member="sam@example.edu",
        member_name="Sam Example",
        task_code="M06",
        subsystem=subsystem,
        did=did,
        why=why,
        original=did,
        posted_at=kw.pop("posted_at", DAY),
        **kw,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def make_decision(session, subsystem="Enclosure", **kw):
    values = {
        "subsystem": subsystem,
        "topic": "Gasket hole clearance",
        "decision": "0.050 in",
        "rationale": "The March doc supersedes the February calculations.",
        "sources": [
            {"title": "Crush Documentation", "link": "https://docs.google.com/document/d/abc/edit"},
            {"title": "Seal Calculations", "link": "/decisions/3"},
        ],
        "status": "confirmed",
        "proposed_by": "Lee Lead",
        "confirmed_by": "Lee Lead",
        "confirmed_at": DAY,
    }
    values.update(kw)
    decision = Decision(**values)
    session.add(decision)
    session.commit()
    session.refresh(decision)
    return decision


def only_row(session) -> DriveWrite:
    return session.exec(select(DriveWrite)).one()


# --- where an entry goes ---------------------------------------------------------


def test_a_note_goes_under_its_tab_and_a_new_date_heading(session, doc):
    note = make_note(session, did="Tack-welded the four walls", why="opposite corners first")

    row = drive_write.write_note(session, note, doc)

    assert row.status == "written" and row.tab_title == "Enclosure" and not row.used_general
    assert doc.lines("Enclosure") == [
        HEADING,
        "Sam Example · M06: Tack-welded the four walls. Why: opposite corners first.",
    ]
    assert doc.style_of("Enclosure", HEADING) == "HEADING_3"
    assert doc.lines("Seals") == [""]  # nothing leaked into other tabs


def test_a_second_note_the_same_day_joins_the_existing_heading(session, doc):
    first = make_note(session, did="Cut the panels.")
    second = make_note(session, did="Deburred them.")

    drive_write.write_note(session, first, doc)
    drive_write.write_note(session, second, doc)

    assert doc.lines("Enclosure") == [
        HEADING,
        "Sam Example · M06: Cut the panels.",
        "Sam Example · M06: Deburred them.",
    ]


def test_a_note_without_a_why_has_no_why(session, doc):
    drive_write.write_note(session, make_note(session, did="Sanded the lid", why=""), doc)

    assert doc.lines("Enclosure")[-1] == "Sam Example · M06: Sanded the lid."


def test_a_later_day_gets_its_own_heading_below(session, doc):
    drive_write.write_note(session, make_note(session, did="Monday work."), doc)
    tomorrow = make_note(session, did="Tuesday work.", posted_at=at(18, 20))

    drive_write.write_note(session, tomorrow, doc)

    assert doc.lines("Enclosure") == [
        HEADING,
        "Sam Example · M06: Monday work.",
        "18 Nov 2026",
        "Sam Example · M06: Tuesday work.",
    ]


def test_an_older_days_entry_goes_under_its_own_heading_not_the_end(session, doc):
    """A retry the next day still lands under the day the work was done."""
    drive_write.write_note(session, make_note(session, did="Monday work."), doc)
    drive_write.write_note(
        session,
        make_note(session, did="Tuesday work.", posted_at=at(18, 20)),
        doc,
    )
    late = make_note(session, did="Monday, written late.")

    drive_write.write_note(session, late, doc)

    assert doc.lines("Enclosure") == [
        HEADING,
        "Sam Example · M06: Monday work.",
        "Sam Example · M06: Monday, written late.",
        "18 Nov 2026",
        "Sam Example · M06: Tuesday work.",
    ]
    assert doc.style_of("Enclosure", "Sam Example · M06: Monday, written late.") == "NORMAL_TEXT"
    assert doc.style_of("Enclosure", "18 Nov 2026") == "HEADING_3"


def test_the_date_is_the_teams_local_day_not_utc(session, doc):
    late_evening = make_note(session, posted_at=at(18, 3))  # 7:30 pm on the 17th

    drive_write.write_note(session, late_evening, doc)

    assert doc.lines("Enclosure")[0] == "17 Nov 2026"


def test_a_tab_with_existing_text_is_added_to_not_changed(session):
    doc = FakeCrushDoc()
    doc.add_tab("Scratch", "Old notes\n16 Nov 2026\nSomeone: earlier entry.")
    doc.add_tab("Enclosure", "Enclosure log\n16 Nov 2026\nSomeone · M01: earlier entry.")
    doc.set_style("Enclosure", "16 Nov 2026", "HEADING_3")
    before = doc.snapshot()

    drive_write.write_note(session, make_note(session), doc)

    after = doc.snapshot()
    assert after["Scratch"] == before["Scratch"]
    new_text = after["Enclosure"]
    assert new_text.startswith("Enclosure log\n16 Nov 2026\nSomeone · M01: earlier entry.")
    # Removing the one inserted block gives back exactly what was there.
    inserted = f"\n{HEADING}\nSam Example · M06: Tack-welded the four walls."
    assert new_text.replace(inserted, "", 1) == before["Enclosure"]


def test_an_edit_only_adds_text_it_never_deletes_or_replaces(session, doc):
    drive_write.write_note(session, make_note(session), doc)
    drive_write.write_answer(session, make_decision(session), doc)

    kinds = {name for batch in doc.sent for request in batch for name in request}
    assert kinds <= {"insertText", "updateParagraphStyle", "updateTextStyle", "deleteParagraphBullets"}
    assert not any("delete" in k.lower() and k != "deleteParagraphBullets" for k in kinds)
    inserts = [r["insertText"] for batch in doc.sent for r in batch if "insertText" in r]
    assert len(inserts) == 2


def test_new_text_is_plain_even_after_a_bold_answer(session, doc):
    drive_write.write_answer(session, make_decision(session), doc)
    drive_write.write_note(session, make_note(session, did="Faced the lid."), doc)

    assert doc.bold_text("Enclosure") == "Gasket hole clearance: 0.050 in"
    assert doc.style_of("Enclosure", "Sam Example · M06: Faced the lid.") == "NORMAL_TEXT"


# --- a recorded answer -------------------------------------------------------------


def test_an_answer_has_a_bold_statement_why_what_it_replaces_sources_and_who(session, doc):
    old = make_decision(session, decision="0.030 in", status="superseded")
    new = make_decision(session)
    old.superseded_by_id = new.id
    session.add(old)
    session.add(Task(code="M03", title="Seal the lid", subsystem="Enclosure", task_type="Design", stage="Concepts"))
    session.commit()
    task = session.exec(select(Task)).one()
    session.add(DecisionTask(decision_id=new.id, task_id=task.id))
    session.commit()

    row = drive_write.write_answer(session, new, doc)

    assert row.status == "written"
    assert doc.lines("Enclosure") == [
        HEADING,
        "Gasket hole clearance: 0.050 in",
        "Why: The March doc supersedes the February calculations.",
        "Replaces: 0.030 in",
        "Sources: Crush Documentation, Seal Calculations",
        "Recorded by Lee Lead · M03",
    ]
    assert doc.bold_text("Enclosure") == "Gasket hole clearance: 0.050 in"
    # Only a Google link becomes a link; the app's own relative link is just its title.
    assert doc.links("Enclosure") == {"https://docs.google.com/document/d/abc/edit": "Crush Documentation"}


def test_an_answer_with_nothing_to_replace_or_cite_leaves_those_lines_out(session, doc):
    bare = make_decision(session, rationale="", sources=[])

    drive_write.write_answer(session, bare, doc)

    assert doc.lines("Enclosure") == [HEADING, "Gasket hole clearance: 0.050 in", "Recorded by Lee Lead"]


# --- which tab ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "subsystem, tab",
    [
        ("Enclosure", "Enclosure"),
        ("Seals/Waterproofing", "Seals"),
        ("Thrusters", "Thrusters"),
        ("Gripper", "Actuators"),
        ("Torpedo", "Actuators"),
        ("Electronics Mounts", "Enclosure"),  # no tab of its own: its part of Crush
    ],
)
def test_each_subsystem_finds_its_tab(session, doc, subsystem, tab):
    row = drive_write.write_note(session, make_note(session, subsystem=subsystem), doc)

    assert row.tab_title == tab and not row.used_general
    assert HEADING in doc.lines(tab)


def test_a_tab_named_for_the_subsystem_beats_its_part(session):
    doc = FakeCrushDoc("Enclosure", "Electronics Mounts", "General")

    row = drive_write.write_note(session, make_note(session, subsystem="Electronics Mounts"), doc)

    assert row.tab_title == "Electronics Mounts"


def test_tab_names_ignore_case_and_punctuation(session):
    doc = FakeCrushDoc("seals & waterproofing", "General")

    row = drive_write.write_note(session, make_note(session, subsystem="Seals/Waterproofing"), doc)

    assert row.tab_title == "seals & waterproofing" and not row.used_general


def test_child_tabs_are_found_too(session):
    doc = FakeCrushDoc("Subsystems", "General")
    doc.tabs[0].children.append(doc.add_tab("Thrusters", tab_id="t.child"))
    doc.tabs.pop()  # add_tab appended it to the top level as well

    row = drive_write.write_note(session, make_note(session, subsystem="Thrusters"), doc)

    assert row.tab_title == "Thrusters" and row.tab_id == "t.child"


def test_with_no_matching_tab_the_general_tab_is_used_and_it_says_so(session):
    doc = FakeCrushDoc("Enclosure", "General")
    note = make_note(session, subsystem="Torpedo")

    row = drive_write.write_note(session, note, doc)

    assert row.status == "written" and row.used_general and row.tab_title == "General"
    assert doc.lines("General")[-1].startswith("Sam Example · M06:")
    assert drive_write.status_text(row, "Torpedo") == (
        "Written to the General tab of the Crush document. "
        "There is no Torpedo tab, so it went under General."
    )


def test_with_no_matching_tab_and_no_general_tab_the_entry_is_kept_for_a_retry(session):
    doc = FakeCrushDoc("Enclosure")

    row = drive_write.write_note(session, make_note(session, subsystem="Torpedo"), doc)

    assert row.status == "failed"
    assert "Add a “General” tab" in row.error
    assert doc.sent == []
    doc.add_tab("General")
    note = session.exec(select(WorkNote)).one()
    assert drive_write.retry(session, "note", note.id, doc).status == "written"


# --- once --------------------------------------------------------------------------


def test_writing_the_same_note_twice_adds_it_once(session, doc):
    note = make_note(session)

    first = drive_write.write_note(session, note, doc)
    second = drive_write.write_note(session, note, doc)

    assert first.status == second.status == "written"
    assert doc.tab("Enclosure").text().count("Tack-welded") == 1
    assert len(doc.sent) == 1
    assert len(session.exec(select(DriveWrite)).all()) == 1


def test_a_write_that_reached_the_document_but_was_never_recorded_is_not_repeated(session, doc):
    """The app lost the answer (a crash after Google said yes): the retry sees the text."""
    note = make_note(session)
    drive_write.write_note(session, note, doc)
    row = only_row(session)
    row.status, row.error = "failed", "Couldn't reach Google Docs."
    session.add(row)
    session.commit()

    again = drive_write.retry(session, "note", note.id, doc)

    assert again.status == "written"
    assert doc.tab("Enclosure").text().count("Tack-welded") == 1


def test_a_write_already_under_way_is_left_alone(session, doc):
    note = make_note(session)
    session.add(DriveWrite(kind="note", ref_id=note.id, status="writing", updated_at=utc_now() + timedelta(days=1)))
    session.commit()

    row = drive_write.write_note(session, note, doc)

    assert row.status == "writing"
    assert doc.sent == [] and doc.gets == 0


def test_an_abandoned_write_is_taken_over(session, doc):
    note = make_note(session)
    session.add(DriveWrite(kind="note", ref_id=note.id, status="writing", updated_at=at(1, 0, month=1)))
    session.commit()

    row = drive_write.write_note(session, note, doc)

    assert row.status == "written"


# --- failure ------------------------------------------------------------------------


def test_a_google_failure_keeps_the_entry_and_says_why_without_leaking_anything(session, doc):
    note = make_note(session)
    doc.fail_update = http_error(500, "backend exploded: secret-token-123")

    row = drive_write.write_note(session, note, doc)

    assert row.status == "failed" and row.attempts == 1
    assert "secret-token-123" not in row.error
    assert "500" in row.error
    assert session.get(WorkNote, note.id) is not None
    assert doc.tab("Enclosure").text() == "\n"


def test_a_failed_write_can_be_retried_and_lands_once(session, doc):
    note = make_note(session)
    doc.fail_get = http_error(503, "unavailable")
    assert drive_write.write_note(session, note, doc).status == "failed"
    doc.fail_get = None

    row = drive_write.retry(session, "note", note.id, doc)

    assert row.status == "written" and row.attempts == 2 and row.error is None
    assert doc.tab("Enclosure").text().count("Tack-welded") == 1


@pytest.mark.parametrize(
    "status, words",
    [(403, "editor"), (404, "DECISIONS_DOC_ID"), (429, "rate limit")],
)
def test_the_usual_google_failures_have_plain_messages(session, doc, status, words):
    doc.fail_get = http_error(status, "nope")

    row = drive_write.write_note(session, make_note(session), doc)

    assert words in row.error


def test_an_unexpected_error_is_a_failed_write_not_a_crash(session, doc):
    class Broken:
        def documents(self):
            raise RuntimeError("secret-token-123 in the message")

    row = drive_write.write_note(session, make_note(session), Broken())

    assert row.status == "failed"
    assert "secret-token-123" not in row.error


def test_without_a_document_id_nothing_is_attempted(session, doc, monkeypatch):
    monkeypatch.delenv("DECISIONS_DOC_ID")

    row = drive_write.write_note(session, make_note(session), doc)

    assert row.status == "failed" and row.error == drive_write.NOT_CONFIGURED
    assert doc.gets == 0
    assert drive_write.doc_link() is None


def test_missing_google_credentials_are_a_failed_write(session, monkeypatch):
    monkeypatch.undo()  # back to the real build_docs (the autouse fixture's guard)
    for name in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DECISIONS_DOC_ID", DOC_ID)

    row = drive_write.write_note(session, make_note(session), None)

    assert row.status == "failed" and "credentials are not configured" in row.error


def test_when_the_document_changed_since_it_was_read_the_position_is_recomputed(session, doc):
    note = make_note(session)
    edits = []

    def someone_types(fake):
        if not edits:
            edits.append(1)
            fake.prepend("Enclosure", "A new first line")

    doc.before_update = someone_types

    row = drive_write.write_note(session, note, doc)

    assert row.status == "written" and doc.gets == 2
    assert doc.lines("Enclosure") == [
        "A new first line",
        HEADING,
        "Sam Example · M06: Tack-welded the four walls.",
    ]


def test_a_document_that_never_stops_changing_is_a_failed_write(session, doc):
    def always(fake):
        fake.revision += 1

    doc.before_update = always

    row = drive_write.write_note(session, make_note(session), doc)

    assert row.status == "failed"
    assert doc.tab("Enclosure").text() == "\n"


# --- scope and links -----------------------------------------------------------------


def test_the_write_scope_is_asked_for_only_here_and_calls_time_out(monkeypatch):
    monkeypatch.undo()
    asked, built = [], {}

    def fake_credentials(scopes=None):
        asked.append(scopes)
        return object()

    def fake_build(api, version, **kwargs):
        built.update(api=api, version=version, **kwargs)
        return "docs-client"

    monkeypatch.setattr(drive, "_load_credentials", fake_credentials)
    monkeypatch.setattr(drive_write.google_auth_httplib2, "AuthorizedHttp", lambda creds, http: http)
    monkeypatch.setattr(drive_write, "build", fake_build)

    assert drive_write.build_docs() == "docs-client"
    assert asked == [[drive_write.DOCS_WRITE_SCOPE]]
    assert (built["api"], built["version"]) == ("docs", "v1")
    assert built["http"].timeout == drive_write.REQUEST_TIMEOUT_SECONDS


def test_no_other_module_names_a_google_write_scope():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1]
    offenders = []
    for path in src.rglob("*.py"):
        if path.parts[-2:-1] == ("tests",) or path.name == "drive_write.py":
            continue
        text = path.read_text()
        scopes = re.findall(r"https://www\.googleapis\.com/auth/[\w.]+", text)
        if any(not scope.endswith("readonly") for scope in scopes):
            offenders.append(path.name)
    assert offenders == []


def test_the_document_link_goes_to_the_tab(monkeypatch):
    assert drive_write.doc_link() == f"https://docs.google.com/document/d/{DOC_ID}/edit"
    assert drive_write.doc_link("t.2").endswith("/edit?tab=t.2")


# --- review fixes ----------------------------------------------------------------------------


def test_two_notes_with_the_same_words_are_two_notes(session, doc):
    """Only a retry looks for text already there; a first attempt always adds."""
    first = make_note(session, did="Sanded the lid.")
    again = make_note(session, did="Sanded the lid.")

    drive_write.write_note(session, first, doc)
    drive_write.write_note(session, again, doc)

    assert doc.tab("Enclosure").text().count("Sanded the lid.") == 2


def test_text_inserted_before_a_bold_heading_does_not_take_its_look(session, doc):
    drive_write.write_note(session, make_note(session, did="Monday work."), doc)
    drive_write.write_note(session, make_note(session, did="Tuesday work.", posted_at=at(18, 20)), doc)
    doc.make_bold("Enclosure", "18 Nov 2026")

    drive_write.write_note(session, make_note(session, did="Monday, late."), doc)

    assert "Monday, late" not in doc.bold_text("Enclosure")
    resets = [
        r["updateTextStyle"]
        for batch in doc.sent
        for r in batch
        if "updateTextStyle" in r and r["updateTextStyle"]["textStyle"] == {}
    ]
    assert resets and all(
        {"bold", "link", "fontSize", "foregroundColor"} <= set(r["fields"].split(",")) for r in resets
    )


def test_only_a_recorded_answer_is_ever_written_even_when_asked_to_retry(session, doc):
    for status in ("proposed", "superseded"):
        decision = make_decision(session, status=status)

        assert drive_write.retry(session, "answer", decision.id, doc) is None

    assert doc.gets == 0 and session.exec(select(DriveWrite)).all() == []


def test_the_date_heading_does_not_depend_on_the_servers_locale():
    from datetime import date

    import localtime

    assert [localtime.heading(date(2026, m, 3)) for m in (1, 5, 9, 12)] == [
        "3 Jan 2026", "3 May 2026", "3 Sep 2026", "3 Dec 2026",
    ]
