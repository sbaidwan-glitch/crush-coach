"""Sign in on a Railway PR environment without Google: `GET /dev-login?token=...&as=member|lead`.

Preview environments can't use Google OAuth (each PR has its own URL), so this route sets the
same signed session a real sign-in sets, for one of two made-up people. It is off unless ALL
of these are true, checked on every request (so turning a variable off ends the sessions too):

- `DEV_LOGIN=1`
- `DEV_LOGIN_TOKEN` is set and not empty, and the `token` in the URL matches it (constant time)
- `RAILWAY_ENVIRONMENT_NAME` is set and is not "production" (any case)

When it is off the route answers 404, exactly like an unknown URL. Nothing here logs the
token, and the access log line for this URL has its token blanked (`RedactDevToken`).
"""

import hmac
import logging
import os
import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

MEMBER_EMAIL = "preview.member@example.test"
LEAD_EMAIL = "preview.lead@example.test"
# email -> (name, role). Only these two identities exist, and only while `enabled()`.
IDENTITIES = {
    MEMBER_EMAIL: ("Preview Member", "member"),
    LEAD_EMAIL: ("Preview Lead", "lead"),
}
WHO = {"member": MEMBER_EMAIL, "lead": LEAD_EMAIL}


def _token() -> str:
    return os.environ.get("DEV_LOGIN_TOKEN", "")


def enabled() -> bool:
    """True only on a non-production Railway environment with the switch and a token set."""
    if os.environ.get("DEV_LOGIN", "").strip() != "1":
        return False
    if not _token():
        return False
    environment = os.environ.get("RAILWAY_ENVIRONMENT_NAME", "").strip().casefold()
    return bool(environment) and environment != "production"


def token_matches(candidate: str) -> bool:
    """Constant-time compare of the URL's token with `DEV_LOGIN_TOKEN` (never both empty)."""
    expected = _token().encode("utf-8")
    return bool(expected) and hmac.compare_digest(candidate.encode("utf-8"), expected)


def role_for(email: str) -> str | None:
    """The role of a preview identity while dev login is on; None for anyone else.

    `auth.session_user` asks this on every request, so these two people are accepted (the
    lead as a lead) without touching ALLOWED_EMAILS or LEAD_EMAILS, and stop being
    accepted the moment dev login is off.
    """
    identity = IDENTITIES.get(email.strip().casefold())
    if identity is None or not enabled():
        return None
    return identity[1]


@router.get("/dev-login", include_in_schema=False)
def dev_login(
    request: Request,
    token: str = "",
    who: Annotated[str, Query(alias="as")] = "member",
) -> RedirectResponse:
    if not enabled() or not token_matches(token):
        raise HTTPException(status_code=404)  # the same answer as an unknown URL
    if who not in WHO:
        raise HTTPException(status_code=400, detail="as must be member or lead")

    email = WHO[who]
    name, role = IDENTITIES[email]
    request.session.clear()
    request.session["user"] = {"name": name, "email": email, "role": role}
    # Redirect so the token doesn't stay in the address bar or the browser history.
    return RedirectResponse(
        "/tasks", status_code=303, headers={"Cache-Control": "no-store"}
    )


_TOKEN_IN_URL = re.compile(r"([?&]token=)[^&\s\"']*")


class RedactDevToken(logging.Filter):
    """Blank `token=...` in uvicorn's access log, which prints the whole request URL."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_IN_URL.sub(r"\1[redacted]", arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(RedactDevToken())
