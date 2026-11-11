"""Screenshot the main pages at phone and desktop width: `make screenshots`.

    cd src && ../.venv/bin/python scripts/screenshots.py

Starts the app against a throwaway SQLite database seeded with the made-up sample
tasks, signs in with a forged session cookie for a made-up member, and saves
/, /tasks, /tasks/M04 and the decision pages at 375px and 1280px into
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
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
OUT = ROOT / ".context" / "screenshots"
WIDTHS = (375, 1280)
# (file name, path, who is signed in)
PAGES = (
    ("home", "/", "member"),
    ("tasks", "/tasks", "member"),
    ("task-M04", "/tasks/M04", "member"),
    ("decisions", "/decisions", "member"),
    ("decision-confirmed", "/decisions/1", "member"),
    ("decision-proposed", "/decisions/2", "member"),
    ("decision-new", "/decisions/new?task=M04", "member"),
    ("decision-proposed-lead", "/decisions/2", "lead"),
    ("decision-confirmed-lead", "/decisions/1", "lead"),
)

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
    from models import CoachMessage, Decision, DecisionTask, Task, utc_now

    assert db.database_url().startswith("sqlite"), (
        "refusing to seed a non-SQLite database"
    )
    db.run_migrations()
    with Session(db.engine) as session:
        import_tasks.import_tasks(session, SRC / "sample_data" / "tasks.csv")
        task = session.exec(select(Task).where(Task.code == "M04")).one()
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
        # Two made-up decisions: one confirmed, one waiting for a lead.
        latch = Decision(
            subsystem=task.subsystem,
            topic="Lid gasket style",
            decision="Use a face-seal O-ring on the lid.",
            rationale="It seals evenly under pressure and we can buy standard sizes.",
            alternatives=[
                {
                    "option": "Flat gasket",
                    "rejected_because": "Hard to keep compressed evenly across the lid.",
                }
            ],
            sources=[
                {
                    "title": "O-ring groove notes (example)",
                    "link": "https://docs.google.com/document/d/example",
                    "folder": "Crush / Seals",
                    "year": 2026,
                }
            ],
            status="confirmed",
            proposed_by=MEMBER["email"],
            confirmed_by=LEAD["email"],
            confirmed_at=utc_now(),
        )
        proposal = Decision(
            subsystem=task.subsystem,
            topic="Lid latch style",
            decision="Use two cam latches on the long sides.",
            rationale="Toggle latches loosen under vibration.",
            alternatives=[
                {"option": "Toggle latch", "rejected_because": "Loosens under vibration."}
            ],
            sources=[],
            status="proposed",
            proposed_by=MEMBER["email"],
        )
        session.add_all([latch, proposal])
        session.flush()
        session.add(DecisionTask(decision_id=latch.id, task_id=task.id))
        session.add(DecisionTask(decision_id=proposal.id, task_id=task.id))
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
                    for who, user in (("member", MEMBER), ("lead", LEAD)):
                        context = browser.new_context(
                            viewport={"width": width, "height": 900}
                        )
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
                        if response.status != 200:
                            problems.append(f"{path} returned {response.status}")
                        if overflow > 0:
                            problems.append(
                                f"{path} scrolls sideways by {overflow}px at {width}px"
                            )
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
