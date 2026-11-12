import os

import pytest
from fake_google import (
    DOC,
    FOLDER,
    PDF,
    SHEET,
    FakeGoogle,
    doc_paragraph,
    doc_tab,
    make_pdf,
)

# Must be set before `main`/`db` are imported: tests run on in-memory SQLite.
# Assigned (not setdefault) so a developer's .env can never point tests at Postgres.
os.environ["DATABASE_URL"] = "sqlite://"
# A made-up key: tests never reach the network, but the code checks it is set.
os.environ["GLASSROOM_API_KEY"] = "sk-test-not-a-real-key"
os.environ["SESSION_SECRET"] = "test-only-session-secret"
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client-id"
os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-client-secret"
os.environ["ALLOWED_EMAILS"] = "member@example.edu"
os.environ["ALLOWED_DOMAIN"] = ""
os.environ["LEAD_EMAILS"] = ""


@pytest.fixture
def session():
    """A session on the shared in-memory database with empty task, coach, decision and
    Drive index tables.

    The in-memory database lives for the whole test run, so rows would leak
    between tests; clear them before and after.
    """
    from sqlmodel import Session, delete

    import db
    from models import (
        CoachMessage,
        Decision,
        DecisionTask,
        DriveChunk,
        DriveFile,
        LlmCall,
        SyncState,
        Task,
        TaskEvent,
        TaskRemoval,
    )

    def clear(s):
        # Children first: they all point at task (or at decision, or at a file).
        for table in (
            DecisionTask,
            Decision,
            TaskEvent,
            CoachMessage,
            LlmCall,
            Task,
            TaskRemoval,
            DriveChunk,
            DriveFile,
            SyncState,
        ):
            s.exec(delete(table))
        s.commit()

    db.run_migrations()
    with Session(db.engine) as s:
        clear(s)
        yield s
        s.rollback()
        clear(s)


@pytest.fixture
def fake():
    """A made-up team drive: MuddSub / Mechanical / (CAD, Docs)."""
    g = FakeGoogle()
    g.add_drive("drv1", "MuddSub")
    g.add("fMech", "Mechanical", FOLDER, "drv1")
    g.add("fCad", "CAD", FOLDER, "fMech")
    g.add("fDocs", "Docs", FOLDER, "fMech")

    g.add(
        "docLid",
        "Lid latch design notes",
        DOC,
        "fDocs",
        modified="2026-10-05T09:00:00Z",
    )
    g.documents["docLid"] = {
        "tabs": [
            doc_tab(
                "Overview",
                doc_paragraph("Lid latch notes", "HEADING_1"),
                doc_paragraph("The lid latch holds the enclosure lid closed."),
            ),
            doc_tab(
                "Testing",
                doc_paragraph("Leak test", "HEADING_1"),
                doc_paragraph("Pressurize and watch the gauge."),
            ),
        ]
    }
    g.add("sheetParts", "Parts list", SHEET, "fDocs", modified="2026-09-20T09:00:00Z")
    g.spreadsheets["sheetParts"] = [
        ("Fasteners", [["Part", "Qty"], ["M3 screw", "40"], ["Lid latch", "2"]]),
        ("Seals", [["Part", "Qty"], ["O-ring 042", "6"]]),
    ]
    g.add("pdfData", "Latch datasheet.pdf", PDF, "fDocs", size="2048")
    g.media["pdfData"] = make_pdf("Latch rated for 40 N preload", "Second page text")
    g.add("pdfBig", "Huge scan.pdf", PDF, "fDocs", size=str(30 * 1024 * 1024))
    g.add(
        "cadOld",
        "Enclosure_v1.SLDPRT",
        "application/octet-stream",
        "fCad",
        modified="2026-08-01T10:00:00Z",
        size="5000",
    )
    g.add(
        "cadNew",
        "Enclosure_v2.SLDPRT",
        "application/octet-stream",
        "fCad",
        modified="2026-10-09T10:00:00Z",
        size="5200",
    )
    g.add("stl", "lid_mount.stl", "model/stl", "fCad")
    g.add(
        "word",
        "Old minutes.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "fDocs",
    )
    return g


@pytest.fixture(autouse=True)
def real_demote_rules():
    """Search ranks old seasons lower, which depends on today's date. Tests that are
    not about that get an empty rule set so they can't drift with the calendar; the
    ones that are about it pass `demote=` and `today=` explicitly. Yields the real
    function for the one test that wants the config file.

    (Patched by hand, not with `monkeypatch`: sharing that fixture would change when
    other tests' patches are undone relative to the database cleanup.)
    """
    import drive_index
    import drive_rank

    real = drive_index.demote_rules
    drive_index.demote_rules = lambda: drive_rank.DemoteRules.none()
    yield real
    drive_index.demote_rules = real
