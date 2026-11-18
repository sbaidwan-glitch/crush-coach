"""Screenshot the main pages at phone and desktop width: `make screenshots`.

    cd src && ../.venv/bin/python scripts/screenshots.py

Starts the app against a throwaway SQLite database seeded with the made-up sample
tasks, signs in with a forged session cookie for a made-up member, and saves
/, /tasks, /tasks/E01 (a member's own task in progress), /tasks/G01 (waiting for a lead),
/tasks/G01 as a member (a conflicting coach answer with "Ask a lead to settle this"), the lead Inbox (one card of each kind), the Record, /admin and /admin/import at 375px and 1280px into
.context/screenshots/ (gitignored).
It never touches your real database, your .env secrets, the model or Google.

One-time setup: `.venv/bin/pip install -r requirements-dev.txt`
and `.venv/bin/playwright install chromium`.
"""

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
OUT = ROOT / ".context" / "screenshots"
WIDTHS = (375, 1280)
# (file name, path, who is signed in)
PAGES = (
    ("home", "/", "member"),
    ("home-lead", "/", "lead"),
    ("tasks", "/tasks", "lead"),
    ("tasks-member", "/tasks", "member"),
    ("task-E01", "/tasks/E01", "member"),
    ("task-G01-lead", "/tasks/G01", "lead"),
    ("task-M04", "/tasks/M04", "member"),
    ("task-G01-member", "/tasks/G01", "member"),
    ("inbox", "/inbox", "lead"),
    ("inbox-edit", "/inbox?edit=1", "lead"),
    ("record", "/record", "member"),
    ("record-seals", "/record?part=seals", "member"),
    ("record-empty", "/record?part=thrusters", "member"),
    ("record-notes", "/record?notes=1", "member"),
    ("record-notes-lead", "/record?notes=1", "lead"),
    # The work log as a page (the side panel shows the same content; see DRAWERS)
    ("worklog", "/worklog?task=E01", "member"),
    ("worklog-notebook", "/worklog?tab=notebook", "member"),
    ("admin", "/admin", "lead"),
    ("admin-import", "/admin/import", "lead"),
    # Signed out: a read-only preview (the task and decision pages are a sign-in prompt, 401)
    ("home-guest", "/", "guest"),
    ("tasks-guest", "/tasks", "guest"),
    ("task-guest", "/tasks/E01", "guest"),
    ("record-guest", "/record", "guest"),
)
EXPECTED_STATUS = {"task-guest": 401, "record-guest": 401}
# Pages where the work log button is pressed, and the panel (a drawer) is photographed open.
DRAWERS = (("drawer-task", "/tasks/E01"), ("drawer-home", "/"))

# Made-up values; nothing here is a real secret, person or team data.
SESSION_SECRET = "screenshots-only-not-a-secret"
MEMBER = {"name": "Riley Sample", "email": "member@example.edu", "role": "member"}
LEAD = {"name": "Lee Sample", "email": "lead@example.edu", "role": "lead"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def session_cookie(user: dict[str, str]) -> str:
    """A Starlette session cookie: base64 JSON signed with the session secret."""
    from itsdangerous import TimestampSigner

    payload = base64.b64encode(json.dumps({"user": user}).encode("utf-8"))
    return TimestampSigner(SESSION_SECRET).sign(payload).decode("utf-8")


def seed() -> None:
    """Create the schema, import the sample tasks, add one made-up coach exchange."""
    sys.path.insert(0, str(SRC))
    from sqlmodel import Session, select

    import db
    import import_tasks
    from models import (
        AnswerRequest,
        CoachMessage,
        Decision,
        DecisionTask,
        DriveWrite,
        SyncState,
        Task,
        TaskEvent,
        TaskSuggestion,
        WorkNote,
        utc_now,
    )

    assert db.database_url().startswith("sqlite"), (
        "refusing to seed a non-SQLite database"
    )
    db.run_migrations()
    with Session(db.engine) as session:
        import_tasks.import_tasks(session, SRC / "sample_data" / "tasks.csv")
        task = session.exec(select(Task).where(Task.code == "M04")).one()
        # The signed-in member's own task, with a coach answer that ends in "Next step:".
        mine = session.exec(select(Task).where(Task.code == "E01")).one()
        mine.owner = MEMBER["name"]
        waiting = session.exec(select(Task).where(Task.code == "G01")).one()
        waiting.status = "needs_review"
        now = utc_now()
        session.add_all(
            [
                TaskEvent(
                    task_id=mine.id,
                    actor=MEMBER["name"],
                    from_status="todo",
                    to_status="in_progress",
                    created_at=now - timedelta(days=8),
                ),
                TaskEvent(
                    task_id=waiting.id,
                    actor="Jordan Demo",
                    from_status="in_progress",
                    to_status="needs_review",
                    created_at=now - timedelta(days=2),
                ),
                TaskEvent(
                    task_id=waiting.id,
                    actor=LEAD["name"],
                    from_status="needs_review",
                    to_status="in_progress",
                    note="Check the jaw spacing against the drawing.",
                    created_at=now - timedelta(days=4),
                ),
                CoachMessage(
                    task_id=mine.id,
                    member=MEMBER["email"],
                    role="user",
                    content="How do I start the tray layout?",
                ),
                CoachMessage(
                    task_id=mine.id,
                    member="coach",
                    role="assistant",
                    content=(
                        "Start from the current electronics tray sketch and check each "
                        "board against the component sheet. The decision below is "
                        "confirmed, so use it.\n\n"
                        "Next step: open the tray sub-assembly and check it against "
                        "the electrical component sheet."
                    ),
                    meta=json.dumps(
                        {
                            "sources": [
                                {
                                    "title": "Electrical component sheet (example)",
                                    "link": "https://docs.google.com/spreadsheets/d/example",
                                    "modified": "2026-10-20",
                                }
                            ],
                            "notes": [],
                        }
                    ),
                ),
            ]
        )
        meta = {
            "sources": [
                {
                    "title": "O-ring groove notes (example)",
                    "link": "https://docs.google.com/document/d/example",
                    "modified": "2026-10-02",
                }
            ],
            "notes": [],
        }
        session.add(
            CoachMessage(
                task_id=task.id,
                member=MEMBER["email"],
                role="user",
                content="Which gasket profile should we use for the lid?",
            )
        )
        session.add(
            CoachMessage(
                task_id=task.id,
                member="coach",
                role="assistant",
                content=(
                    "An O-ring is the confirmed choice for the lid. I can't give a groove "
                    "depth without a source you can open, so take it from the document "
                    "below. Your lead approves the drawing, not me."
                ),
                meta=json.dumps(meta),
            )
        )
        # Made-up recorded answers (the Record): one replaces an older one.
        old = Decision(
            subsystem="Seals/Waterproofing",
            topic="Baseplate gasket hole clearance",
            decision="0.030 in",
            rationale="",
            status="superseded",
            proposed_by=LEAD["name"],
            confirmed_by=LEAD["name"],
            confirmed_at=now - timedelta(days=9),
        )
        session.add(old)
        session.flush()
        gasket = Decision(
            subsystem="Seals/Waterproofing",
            topic="Baseplate gasket hole clearance",
            decision="0.050 in",
            rationale="From Crush Documentation, 2026-03-30.",
            sources=[
                {
                    "title": "Crush Documentation (example)",
                    "link": "https://docs.google.com/document/d/example",
                    "folder": "",
                    "year": None,
                }
            ],
            status="confirmed",
            proposed_by=LEAD["name"],
            confirmed_by=LEAD["name"],
            confirmed_at=now,
        )
        latch = Decision(
            subsystem=task.subsystem,
            topic="Lid latch",
            decision="McMaster 6082A12 draw latches.",
            rationale="Alfie's latches don't fit Crush's corner radius.",
            status="confirmed",
            proposed_by=LEAD["name"],
            confirmed_by=LEAD["name"],
            confirmed_at=now - timedelta(days=3),
        )
        session.add_all([gasket, latch])
        session.flush()
        old.superseded_by_id = gasket.id
        session.add(DecisionTask(decision_id=latch.id, task_id=task.id))
        # What waits on a lead: one answer to settle with options, one nothing answers,
        # and a suggested task. (G01 above is the work to check.)
        session.add_all(
            [
                AnswerRequest(
                    task_id=task.id,
                    task_code=task.code,
                    subsystem="Seals/Waterproofing",
                    question="Baseplate gasket hole clearance",
                    asked_by=MEMBER["name"],
                    kind="conflict",
                    options=[
                        {
                            "value": "0.050 in",
                            "source": "Crush Documentation",
                            "link": "https://docs.google.com/document/d/example",
                            "date": "2026-03-30",
                        },
                        {
                            "value": "0.030 in",
                            "source": "Seal Calculations",
                            "link": "https://docs.google.com/document/d/example2",
                            "date": "2026-02-10",
                        },
                    ],
                    suggested=0,
                    created_at=now - timedelta(days=1),
                ),
                AnswerRequest(
                    task_id=mine.id,
                    task_code=mine.code,
                    subsystem="Electronics Mounts",
                    question="O-ring lubricant for the window seal",
                    asked_by=MEMBER["name"],
                    kind="none",
                    suggestion="check the Parker handbook, p. 3-15 (a reference, not a team source).",
                    created_at=now - timedelta(hours=5),
                ),
                TaskSuggestion(
                    title="Make a drill jig for the lid latch holes",
                    why="Done when: the jig locates all 8 latch hole pairs from the lid edge.",
                    suggested_by=MEMBER["name"],
                    created_at=now - timedelta(hours=2),
                ),
            ]
        )
        # A coach answer whose sources disagree, so M04 shows "Ask a lead to settle this".
        session.add_all(
            [
                CoachMessage(
                    task_id=waiting.id,
                    member=MEMBER["email"],
                    role="user",
                    content="What jaw spacing should the gripper use?",
                ),
                CoachMessage(
                    task_id=waiting.id,
                    member="coach",
                    role="assistant",
                    content=(
                        "**Likely current: 42 mm** from Gripper design notes (2026-10-04). "
                        "The older sheet says 38 mm. A lead should confirm.\n\n"
                        "Next step: ask a lead to confirm the spacing."
                    ),
                    meta=json.dumps({"sources": [], "notes": []}),
                ),
            ]
        )
        # Made-up work notes (the notebook, History, the Record) and whether each reached the
        # Crush document; one write failed, so "Retry write to Drive" shows.
        notes = [
            WorkNote(
                member=MEMBER["email"], member_name=MEMBER["name"], task_id=mine.id,
                task_code=mine.code, subsystem=mine.subsystem,
                did="Fitted the tray to the sled and marked the four mounting holes.",
                why="Marking in place keeps the holes aligned with the sled rails.",
                original="fitted tray to sled, marked 4 holes", posted_at=now - timedelta(hours=3),
            ),
            WorkNote(
                member=MEMBER["email"], member_name=MEMBER["name"], task_id=task.id,
                task_code=task.code, subsystem=task.subsystem,
                did="Faced both lid edges and deburred them.",
                original="faced both lid edges and deburred them", posted_at=now - timedelta(days=2),
            ),
            WorkNote(
                member="jordan@example.edu", member_name="Jordan Demo", task_id=task.id,
                task_code=task.code, subsystem=task.subsystem,
                did="Cut four wall panels to drawing rev C.", why="Rev C moved the latch holes.",
                original="cut four wall panels", posted_at=now - timedelta(days=4),
            ),
        ]
        session.add_all(notes)
        session.flush()
        session.add_all(
            [
                DriveWrite(kind="note", ref_id=notes[0].id, status="written",
                           tab_title="Enclosure", tab_id="t.0", written_at=now),
                DriveWrite(kind="note", ref_id=notes[1].id, status="failed", attempts=1,
                           error="Google wouldn't let the app write to the Crush document. "
                                 "Share it with the app's service account as an editor."),
                DriveWrite(kind="note", ref_id=notes[2].id, status="written",
                           tab_title="General", tab_id="t.4", used_general=True, written_at=now),
                DriveWrite(kind="answer", ref_id=latch.id, status="written",
                           tab_title="Enclosure", tab_id="t.0", written_at=now),
            ]
        )
        # A made-up Drive sync state, so /admin shows real-looking numbers.
        session.add(
            SyncState(
                id=1,
                last_incremental_sync=utc_now(),
                file_count=412,
                chunk_count=2380,
                error_count=3,
            )
        )
        session.commit()


def wait_for(url: str, tries: int = 100) -> None:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except OSError:
            time.sleep(0.1)
    raise SystemExit(f"The app did not start at {url}")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is not installed. Run:\n"
            "  .venv/bin/pip install -r requirements-dev.txt\n"
            "  .venv/bin/playwright install chromium"
        )
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        os.environ.update(
            DATABASE_URL=f"sqlite:///{tmp}/screenshots.db",
            SESSION_SECRET=SESSION_SECRET,
            SESSION_COOKIE_SECURE="false",
            GLASSROOM_API_KEY="sk-screenshots-not-a-real-key",
            GOOGLE_OAUTH_CLIENT_ID="screenshots-client-id",
            GOOGLE_OAUTH_CLIENT_SECRET="screenshots-client-secret",
            ALLOWED_EMAILS=f"{MEMBER['email']},{LEAD['email']}",
            ALLOWED_DOMAIN="",
            LEAD_EMAILS=LEAD["email"],
            # A placeholder, not a document: only the links on the Record use it.
            DECISIONS_DOC_ID="screenshots-placeholder-document",
        )
        seed()

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port)],
            cwd=SRC,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        OUT.mkdir(parents=True, exist_ok=True)
        problems = []
        try:
            wait_for(f"{base}/health")
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for width in WIDTHS:
                    contexts = {}
                    for who, user in (("member", MEMBER), ("lead", LEAD), ("guest", None)):
                        context = browser.new_context(
                            viewport={"width": width, "height": 900}
                        )
                        if user is not None:
                            context.add_cookies(
                                [
                                    {
                                        "name": "crush_coach_session",
                                        "value": session_cookie(user),
                                        "url": base,
                                    }
                                ]
                            )
                        contexts[who] = context
                    for name, path, who in PAGES:
                        page = contexts[who].new_page()
                        response = page.goto(f"{base}{path}")
                        page.wait_for_load_state("networkidle")
                        page.evaluate("document.fonts.ready")
                        target = OUT / f"{name}-{width}.png"
                        page.screenshot(path=str(target), full_page=True)
                        overflow = page.evaluate(
                            "document.documentElement.scrollWidth - innerWidth"
                        )
                        print(
                            f"{response.status} {path} at {width}px -> {target.relative_to(ROOT)}"
                        )
                        expected = EXPECTED_STATUS.get(name, 200)
                        if response.status != expected:
                            problems.append(f"{path} returned {response.status}")
                        if overflow > 0:
                            problems.append(
                                f"{path} scrolls sideways by {overflow}px at {width}px"
                            )
                        page.close()
                    # The side panel, open: press the top bar button on a task page and on Home.
                    for name, path in DRAWERS:
                        page = contexts["member"].new_page()
                        page.goto(f"{base}{path}")
                        page.wait_for_load_state("networkidle")
                        page.click("[data-worklog-open]")
                        page.wait_for_selector("#worklog[open] textarea")
                        target = OUT / f"{name}-{width}.png"
                        page.screenshot(path=str(target))
                        print(f"opened the work log on {path} at {width}px -> {target.relative_to(ROOT)}")
                        page.close()
                    for context in contexts.values():
                        context.close()
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=10)

    for problem in problems:
        print(f"PROBLEM: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
