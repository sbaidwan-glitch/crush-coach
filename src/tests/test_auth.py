import httpx
import pytest
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import auth
import main


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def mock_google(
    monkeypatch,
    userinfo=None,
    error=None,
    expected_redirect_uri="http://testserver/auth/callback",
):
    async def authorize_redirect(request, redirect_uri):
        assert redirect_uri == expected_redirect_uri
        return RedirectResponse(
            "https://accounts.google.test/authorize", status_code=302
        )

    async def authorize_access_token(request):
        if error is not None:
            raise error
        return {"userinfo": userinfo}

    monkeypatch.setattr(auth.oauth.google, "authorize_redirect", authorize_redirect)
    monkeypatch.setattr(
        auth.oauth.google, "authorize_access_token", authorize_access_token
    )


def sign_in(client, monkeypatch, userinfo, next_path="/tasks"):
    mock_google(monkeypatch, userinfo=userinfo)
    response = client.get("/login", params={"next": next_path}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://accounts.google.test/authorize"
    return client.get("/auth/callback", follow_redirects=False)


def test_tasks_require_sign_in_but_health_and_home_are_public(client):
    response = client.get("/tasks", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Ftasks"
    assert client.get("/health").status_code == 200
    home = client.get("/")
    assert home.status_code == 200
    assert "Sign in with Google" in home.text


def test_forwarded_https_scheme_is_used_for_oauth_callback(monkeypatch):
    mock_google(
        monkeypatch,
        expected_redirect_uri="https://testserver/auth/callback",
    )
    proxy_app = ProxyHeadersMiddleware(main.app, trusted_hosts="*")

    with TestClient(proxy_app) as proxy_client:
        response = proxy_client.get(
            "/login",
            headers={"X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )

    assert response.status_code == 302


def test_explicitly_allowed_member_can_sign_in(client, monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", " OTHER@EXAMPLE.EDU, member@example.edu ")
    response = sign_in(
        client,
        monkeypatch,
        {
            "email": "Member@Example.edu",
            "email_verified": True,
            "name": "Made Up Member",
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/tasks"
    page = client.get("/tasks")
    assert page.status_code == 200
    assert "Made Up Member" in page.text
    assert 'href="/logout">Log out</a>' in page.text


def test_allowed_domain_member_can_sign_in(client, monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    monkeypatch.setenv("ALLOWED_DOMAIN", "@example.edu")

    response = sign_in(
        client,
        monkeypatch,
        {
            "email": "student@example.edu",
            "email_verified": True,
            "name": "Example Student",
        },
    )

    assert response.status_code == 303
    assert client.get("/tasks").status_code == 200


@pytest.mark.parametrize(
    "userinfo",
    [
        {
            "email": "outsider@other.example",
            "email_verified": True,
            "name": "Example Outsider",
        },
        {
            "email": "student@notexample.edu",
            "email_verified": True,
            "name": "Lookalike Domain",
        },
        {
            "email": "member@example.edu",
            "email_verified": False,
            "name": "Unverified Member",
        },
    ],
)
def test_nonmembers_and_unverified_accounts_are_denied(client, monkeypatch, userinfo):
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    monkeypatch.setenv("ALLOWED_DOMAIN", "example.edu")

    response = sign_in(client, monkeypatch, userinfo)

    assert response.status_code == 403
    assert "Not on the team list" in response.text
    assert client.get("/tasks", follow_redirects=False).status_code == 303


def test_lead_role_comes_from_environment(client, monkeypatch):
    monkeypatch.setenv("LEAD_EMAILS", "member@example.edu")

    response = sign_in(
        client,
        monkeypatch,
        {
            "email": "member@example.edu",
            "email_verified": True,
            "name": "Example Lead",
        },
    )

    assert response.status_code == 303
    assert "Example Lead (lead)" in client.get("/tasks").text


def test_logout_clears_the_session(client, monkeypatch):
    sign_in(
        client,
        monkeypatch,
        {
            "email": "member@example.edu",
            "email_verified": True,
            "name": "Example Member",
        },
    )

    response = client.get("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.get("/tasks", follow_redirects=False).status_code == 303


def test_removing_a_member_from_the_allowlist_revokes_their_session(
    client, monkeypatch
):
    sign_in(
        client,
        monkeypatch,
        {
            "email": "member@example.edu",
            "email_verified": True,
            "name": "Example Member",
        },
    )
    monkeypatch.setenv("ALLOWED_EMAILS", "")

    response = client.get("/tasks", follow_redirects=False)

    assert response.status_code == 303


def test_oauth_failure_has_a_plain_error_and_does_not_log_details(
    client, monkeypatch, caplog
):
    secret_text = "client-secret-must-not-appear"
    mock_google(monkeypatch, error=httpx.ConnectError(secret_text))
    client.get("/login", follow_redirects=False)

    with caplog.at_level("WARNING", logger="crush_coach"):
        response = client.get("/auth/callback")

    assert response.status_code == 503
    assert "Sign-in unavailable" in response.text
    assert "Google sign-in is unavailable right now" in response.text
    assert secret_text not in caplog.text
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200


def test_missing_oauth_configuration_has_a_plain_error(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET")

    response = client.get("/login")

    assert response.status_code == 503
    assert "Google sign-in is not configured right now" in response.text


@pytest.mark.parametrize(
    "next_path", ["https://malicious.example/steal", r"/\malicious.example"]
)
def test_unsafe_post_sign_in_redirect_is_rejected(client, monkeypatch, next_path):
    response = sign_in(
        client,
        monkeypatch,
        {
            "email": "member@example.edu",
            "email_verified": True,
            "name": "Example Member",
        },
        next_path=next_path,
    )

    assert response.headers["location"] == "/tasks"


def test_tampered_session_cookie_is_not_accepted(client, monkeypatch):
    sign_in(
        client,
        monkeypatch,
        {
            "email": "member@example.edu",
            "email_verified": True,
            "name": "Example Member",
        },
    )
    cookie = client.cookies.get("crush_coach_session")
    assert cookie is not None
    replacement = "a" if cookie[0] != "a" else "b"
    client.cookies.clear()
    client.cookies.set("crush_coach_session", f"{replacement}{cookie[1:]}")

    response = client.get("/tasks", follow_redirects=False)

    assert response.status_code == 303
