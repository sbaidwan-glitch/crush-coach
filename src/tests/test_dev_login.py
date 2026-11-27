"""The preview-environment sign-in (src/dev_login.py): off unless every condition holds."""

import logging

import pytest
from fastapi.testclient import TestClient

import dev_login
import main

TOKEN = "preview-token-for-tests-only"


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def preview(monkeypatch):
    """A Railway PR environment with dev login switched on."""
    monkeypatch.setenv("DEV_LOGIN", "1")
    monkeypatch.setenv("DEV_LOGIN_TOKEN", TOKEN)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "crush-coach-pr-12")


def sign_in(client, who="member", token=TOKEN):
    return client.get(
        "/dev-login", params={"token": token, "as": who}, follow_redirects=False
    )


def unknown_url_response(client):
    return client.get("/no-such-page-here", follow_redirects=False)


def test_the_route_is_off_by_default(client):
    response = sign_in(client)

    assert response.status_code == 404
    assert response.json() == unknown_url_response(client).json()
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("name", ["production", "Production", "PRODUCTION", " production ", ""])
def test_the_route_is_off_in_production_or_with_a_blank_name(client, preview, monkeypatch, name):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", name)

    response = sign_in(client)

    assert response.status_code == 404
    assert client.get("/tasks").status_code == 200  # still the signed-out preview
    assert "Sign in with Google" in client.get("/").text


def test_the_route_is_off_when_the_environment_name_is_missing(client, preview, monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME")

    assert sign_in(client).status_code == 404


def test_the_route_is_off_without_the_switch_or_with_another_value(client, preview, monkeypatch):
    monkeypatch.delenv("DEV_LOGIN")
    assert sign_in(client).status_code == 404

    monkeypatch.setenv("DEV_LOGIN", "true")
    assert sign_in(client).status_code == 404


def test_the_route_is_off_when_the_token_is_not_set(client, preview, monkeypatch):
    monkeypatch.setenv("DEV_LOGIN_TOKEN", "")
    assert sign_in(client, token="").status_code == 404

    monkeypatch.delenv("DEV_LOGIN_TOKEN")
    assert sign_in(client, token="").status_code == 404


@pytest.mark.parametrize("token", ["", "wrong", TOKEN + "x", TOKEN.upper(), "tökén"])
def test_a_wrong_token_is_a_404_and_signs_nobody_in(client, preview, token):
    response = sign_in(client, token=token)

    assert response.status_code == 404
    assert response.json() == unknown_url_response(client).json()
    assert client.get("/inbox", follow_redirects=False).status_code == 303  # still signed out


def test_the_token_is_compared_in_constant_time(preview, monkeypatch):
    seen = []
    real = dev_login.hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(dev_login.hmac, "compare_digest", spy)

    assert dev_login.token_matches(TOKEN) is True
    assert dev_login.token_matches("nope") is False
    assert len(seen) == 2


def test_a_member_signs_in_and_can_open_tasks_but_not_the_inbox(client, preview):
    response = sign_in(client, "member")

    assert response.status_code == 303
    assert response.headers["location"] == "/tasks"  # the token is not left in the address bar
    assert response.headers["cache-control"] == "no-store"
    assert TOKEN not in response.headers["location"]
    assert client.get("/tasks").status_code == 200
    assert client.get("/profile").status_code == 200
    assert client.get("/inbox").status_code == 403


def test_the_default_is_member(client, preview):
    response = client.get("/dev-login", params={"token": TOKEN}, follow_redirects=False)

    assert response.status_code == 303
    assert client.get("/inbox").status_code == 403


def test_a_lead_signs_in_and_can_open_the_inbox_without_lead_emails(client, preview, monkeypatch):
    monkeypatch.setenv("LEAD_EMAILS", "")
    monkeypatch.setenv("ALLOWED_EMAILS", "member@example.edu")

    response = sign_in(client, "lead")

    assert response.status_code == 303
    assert client.get("/tasks").status_code == 200
    assert client.get("/inbox").status_code == 200
    assert client.get("/admin").status_code == 200


def test_an_unknown_identity_is_rejected_after_the_token_checks_out(client, preview):
    assert sign_in(client, "admin").status_code == 400


def test_the_session_stops_working_when_dev_login_is_turned_off(client, preview, monkeypatch):
    sign_in(client, "lead")
    assert client.get("/inbox").status_code == 200

    monkeypatch.setenv("DEV_LOGIN", "0")

    assert client.get("/inbox", follow_redirects=False).status_code == 303


def test_the_session_stops_working_if_the_environment_becomes_production(client, preview, monkeypatch):
    sign_in(client, "lead")

    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")

    assert client.get("/inbox", follow_redirects=False).status_code == 303


def test_the_preview_emails_mean_nothing_in_a_forged_session_when_it_is_off(client, monkeypatch):
    """Even a validly signed session for a preview identity is refused while the route is off."""
    monkeypatch.setenv("DEV_LOGIN", "1")
    monkeypatch.setenv("DEV_LOGIN_TOKEN", TOKEN)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "pr-1")
    sign_in(client, "lead")
    monkeypatch.delenv("DEV_LOGIN")

    assert dev_login.role_for(dev_login.LEAD_EMAIL) is None
    assert client.get("/inbox", follow_redirects=False).status_code == 303


def test_the_access_log_does_not_keep_the_token():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", f"/dev-login?token={TOKEN}&as=lead", "1.1", 303), None,
    )

    dev_login.RedactDevToken().filter(record)

    assert TOKEN not in record.getMessage()
    assert "as=lead" in record.getMessage()
    assert "token=[redacted]" in record.getMessage()


def test_the_route_does_not_log_the_token(client, preview, caplog):
    with caplog.at_level(logging.DEBUG):
        sign_in(client, "lead")
        sign_in(client, "lead", token="wrong-token-value")

    # The test client (httpx) logs the URLs it requests; only the app's own records matter.
    app_log = "\n".join(
        record.getMessage() for record in caplog.records if not record.name.startswith("httpx")
    )
    assert TOKEN not in app_log
    assert "wrong-token-value" not in app_log
