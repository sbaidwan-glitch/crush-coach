import os

import pytest

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
    """A session on the shared in-memory database with empty task and coach tables.

    The in-memory database lives for the whole test run, so rows would leak
    between tests; clear them before and after.
    """
    from sqlmodel import Session, delete

    import db
    from models import CoachMessage, LlmCall, Task

    def clear(s):
        # Children first: both tables point at task.
        for table in (CoachMessage, LlmCall, Task):
            s.exec(delete(table))
        s.commit()

    db.run_migrations()
    with Session(db.engine) as s:
        clear(s)
        yield s
        s.rollback()
        clear(s)
