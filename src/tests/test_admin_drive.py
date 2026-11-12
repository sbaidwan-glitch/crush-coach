"""/admin: the lead-only "Sync Drive now" button and the status block."""

import pytest
from fake_google import FakeGoogle
from fastapi.testclient import TestClient
from sqlmodel import select

import auth
import drive_index as di
import main
from models import DriveFile, SyncState

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


def state(session) -> SyncState:
    session.expire_all()
    return session.get(SyncState, 1)


def test_anonymous_is_sent_to_sign_in():
    with make_client(None) as client:
        for method, path in (("get", "/admin"), ("post", "/admin/drive/sync")):
            response = getattr(client, method)(path)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/login")


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/admin"), ("get", "/admin/drive/status"), ("post", "/admin/drive/sync")],
)
def test_member_gets_403_and_no_sync_starts(member, session, monkeypatch, method, path):
    monkeypatch.setattr(
        di, "incremental_sync", lambda *a, **k: pytest.fail("a member started a sync")
    )
    response = getattr(member, method)(path)
    assert response.status_code == 403
    assert session.get(SyncState, 1) is None


def test_page_before_any_sync_says_so_and_offers_the_button(lead):
    page = lead.get("/admin").text
    assert "Sync Drive now" in page
    assert "Never" in page
    assert "Nothing has been synced yet" in page
    assert "hx-get" not in page  # no polling when nothing is running


def test_nav_links_to_admin_for_leads_only():
    with make_client(LEAD) as client:
        assert 'href="/admin"' in client.get("/tasks").text
    with make_client(MEMBER) as client:
        assert 'href="/admin"' not in client.get("/tasks").text
    main.app.dependency_overrides.clear()


def test_button_claims_the_lock_returns_at_once_and_starts_a_background_sync(
    lead, session, monkeypatch
):
    calls = []

    def stub(session_arg, services=None, *, already_claimed=False):
        calls.append(already_claimed)
        # By the time the background job runs, the response is already decided
        # and the lock is held, so the page shows "running".
        assert state(session).running_since is not None
        di.release(session_arg, None)
        return di.SyncReport("incremental")

    monkeypatch.setattr(di, "incremental_sync", stub)

    response = lead.post("/admin/drive/sync")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin?sync=started"
    assert calls == [True]


def test_while_a_sync_runs_the_page_polls_and_the_button_is_off(lead, session):
    di.claim(session)

    page = lead.get("/admin?sync=started").text

    assert 'hx-get="/admin/drive/status"' in page
    assert 'hx-trigger="every 3s"' in page
    assert "disabled" in page and "Syncing…" in page
    assert lead.get("/admin/drive/status").text.count("hx-get") == 1


def test_pressing_the_button_during_a_sync_does_not_start_another(
    lead, session, monkeypatch
):
    monkeypatch.setattr(
        di, "incremental_sync", lambda *a, **k: pytest.fail("a second sync started")
    )
    di.claim(session)

    response = lead.post("/admin/drive/sync")

    assert response.headers["location"] == "/admin?sync=running"
    assert "already running" in lead.get("/admin?sync=running").text


def test_a_finished_background_sync_shows_time_and_counts(
    lead, session, fake, monkeypatch
):
    monkeypatch.setattr(di.GoogleServices, "from_environment", lambda: fake.google())
    di.full_sync(session, fake.google())  # so the button runs an incremental one
    fake.files["docLid"]["modifiedTime"] = "2026-11-03T00:00:00.000Z"
    fake.record_change("docLid")

    lead.post("/admin/drive/sync")  # the test client runs the background task

    st = state(session)
    assert st.running_since is None and st.last_incremental_sync is not None
    page = lead.get("/admin").text
    assert str(st.file_count) in page and str(st.chunk_count) in page
    assert "Never" not in page and "hx-get" not in page
    assert len(session.exec(select(DriveFile)).all()) == st.file_count


def test_a_failed_background_sync_is_shown_and_the_page_still_works(
    lead, session, monkeypatch
):
    def no_credentials():
        raise di.DriveError("Google Drive credentials are not configured.")

    monkeypatch.setattr(di.GoogleServices, "from_environment", no_credentials)

    assert lead.post("/admin/drive/sync").status_code == 303

    st = state(session)
    assert st.running_since is None
    page = lead.get("/admin").text
    assert "The last sync failed" in page
    assert "credentials are not configured" in page
    assert "Sync Drive now" in page  # and the button works again


def test_a_crash_in_the_background_task_never_escapes_and_frees_the_lock(
    lead, session, monkeypatch
):
    def crash(*args, **kwargs):
        raise ValueError("bug")

    monkeypatch.setattr(di, "incremental_sync", crash)

    assert lead.post("/admin/drive/sync").status_code == 303

    st = state(session)
    assert st.running_since is None
    assert "Unexpected error" in st.last_error


def test_page_survives_a_database_failure(lead, monkeypatch):
    import admin

    def broken(session):
        raise RuntimeError("db down")

    monkeypatch.setattr(admin, "_drive_status", broken)
    response = lead.get("/admin")
    assert response.status_code == 200
    assert "Couldn’t read the Drive sync status" in response.text
    assert "Import tasks from the tracker CSV" in response.text  # the rest still works


def test_stale_running_flag_is_not_shown_as_running(lead, session):
    st = di.get_state(session)
    st.running_since = di.utc_now() - di.STALE_LOCK_AFTER - di.timedelta(minutes=5)
    session.add(st)
    session.commit()
    assert "hx-get" not in lead.get("/admin").text


def test_fake_google_fixture_is_importable():
    assert FakeGoogle().token == "t0"


def test_a_stale_lock_does_not_block_the_button(lead, session, monkeypatch):
    """A sync that crashed hours ago must not stop leads syncing forever."""
    st = di.get_state(session)
    st.running_since = di.utc_now() - di.STALE_LOCK_AFTER - di.timedelta(minutes=1)
    session.add(st)
    session.commit()
    calls = []

    def stub(session_arg, services=None, *, already_claimed=False):
        calls.append(already_claimed)
        di.release(session_arg, None)
        return di.SyncReport("incremental")

    monkeypatch.setattr(di, "incremental_sync", stub)

    response = lead.post("/admin/drive/sync")

    assert response.headers["location"] == "/admin?sync=started"
    assert calls == [True]
