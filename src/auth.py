"""Google sign-in, team allowlisting, and route protection."""

import logging
import os
from pathlib import Path
from typing import Annotated, TypedDict
from urllib.parse import urlencode, urlsplit

import httpx
from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

log = logging.getLogger("crush_coach")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()

oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"),
    server_metadata_url=(
        "https://accounts.google.com/.well-known/openid-configuration"
    ),
    client_kwargs={"scope": "openid email profile"},
)


class User(TypedDict):
    name: str
    email: str
    role: str


def _comma_separated(name: str) -> set[str]:
    return {
        value.strip().casefold()
        for value in os.environ.get(name, "").split(",")
        if value.strip()
    }


def email_is_allowed(email: str) -> bool:
    """Return whether an address matches the explicit list or allowed domain."""
    normalized = email.strip().casefold()
    if normalized in _comma_separated("ALLOWED_EMAILS"):
        return True

    domain = os.environ.get("ALLOWED_DOMAIN", "").strip().casefold().lstrip("@")
    _, separator, email_domain = normalized.rpartition("@")
    return bool(domain and separator and email_domain == domain)


def session_user(request: Request) -> User | None:
    """Read and validate the minimal identity stored in the signed session."""
    value = request.session.get("user")
    if not isinstance(value, dict):
        return None

    name = value.get("name")
    email = value.get("email")
    role = value.get("role")
    if not all(isinstance(item, str) and item for item in (name, email, role)):
        return None
    if role not in {"member", "lead"}:
        return None

    normalized_email = email.strip().casefold()
    if not email_is_allowed(normalized_email):
        request.session.clear()
        return None

    current_role = (
        "lead" if normalized_email in _comma_separated("LEAD_EMAILS") else "member"
    )
    user: User = {"name": name, "email": normalized_email, "role": current_role}
    if value != user:
        request.session["user"] = user
    return user


def _safe_next_path(value: object) -> str:
    """Accept local absolute paths only, avoiding an OAuth open redirect."""
    if not isinstance(value, str) or "\\" in value:
        return "/tasks"
    parsed = urlsplit(value)
    if not value.startswith("/") or value.startswith("//"):
        return "/tasks"
    if parsed.scheme or parsed.netloc:
        return "/tasks"
    return value


def require_user(request: Request) -> User:
    """FastAPI dependency that redirects anonymous visitors to Google sign-in."""
    user = session_user(request)
    if user is not None:
        return user

    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    location = f"/login?{urlencode({'next': next_path})}"
    raise HTTPException(status_code=303, headers={"Location": location})


CurrentUser = Annotated[User, Depends(require_user)]


def _sign_in_unavailable(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "auth_error.html",
        {"message": message, "user": session_user(request)},
        status_code=503,
    )


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request, next: str = "/tasks"):
    destination = _safe_next_path(next)
    if session_user(request) is not None:
        return RedirectResponse(destination, status_code=303)

    if not os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or not os.environ.get(
        "GOOGLE_OAUTH_CLIENT_SECRET"
    ):
        return _sign_in_unavailable(
            request,
            "Google sign-in is not configured right now. Please tell a team lead.",
        )

    request.session["post_auth_redirect"] = destination
    redirect_uri = str(request.url_for("auth_callback"))
    try:
        return await oauth.google.authorize_redirect(request, redirect_uri)
    except (OAuthError, httpx.HTTPError) as exc:
        log.warning("Could not start Google sign-in (%s)", type(exc).__name__)
        return _sign_in_unavailable(
            request,
            "Google sign-in is unavailable right now. Please try again in a moment.",
        )


@router.get("/auth/callback", name="auth_callback", response_class=HTMLResponse)
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except (OAuthError, httpx.HTTPError) as exc:
        log.warning("Google sign-in failed (%s)", type(exc).__name__)
        return _sign_in_unavailable(
            request,
            "Google sign-in is unavailable right now. Please try again in a moment.",
        )

    userinfo = token.get("userinfo")
    if not isinstance(userinfo, dict):
        log.warning("Google sign-in returned no user information")
        return _sign_in_unavailable(
            request,
            "Google did not return an account identity. Please try signing in again.",
        )

    email = str(userinfo.get("email", "")).strip().casefold()
    if userinfo.get("email_verified") is not True or not email:
        request.session.clear()
        return templates.TemplateResponse(
            request,
            "not_allowed.html",
            {"user": None},
            status_code=403,
        )

    if not email_is_allowed(email):
        request.session.clear()
        return templates.TemplateResponse(
            request,
            "not_allowed.html",
            {"user": None},
            status_code=403,
        )

    destination = _safe_next_path(request.session.get("post_auth_redirect", "/tasks"))
    role = "lead" if email in _comma_separated("LEAD_EMAILS") else "member"
    name = str(userinfo.get("name", "")).strip() or email
    request.session.clear()
    request.session["user"] = {"name": name, "email": email, "role": role}
    return RedirectResponse(destination, status_code=303)


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=303)
