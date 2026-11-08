import os

import pytest

# Must be set before `main`/`db` are imported: tests run on in-memory SQLite.
# Assigned (not setdefault) so a developer's .env can never point tests at Postgres.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "test-only-session-secret"
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client-id"
os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-client-secret"
os.environ["ALLOWED_EMAILS"] = "member@example.edu"
os.environ["ALLOWED_DOMAIN"] = ""
os.environ["LEAD_EMAILS"] = ""


@pytest.fixture
def session():
    """A session on the shared in-memory database with an empty task table.

    The in-memory database lives for the whole test run, so rows would leak
    between tests; clear them before and after.
    """
    from sqlmodel import Session, delete

    import db
    from models import Task

    db.run_migrations()
    with Session(db.engine) as s:
        s.exec(delete(Task))
        s.commit()
        yield s
        s.rollback()
        s.exec(delete(Task))
        s.commit()
