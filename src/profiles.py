"""Member icons: the icon and short name a member picks at `/profile` (docs/timeline.md).

A member picks one of nine of our own line drawings and a name of up to 20 characters.
The chip (icon in a square) shows next to the owner on task rows and the task page, and
at the tip of their bars on the Timeline. Until a member picks, they show as their Google
first name with the gear icon. No Google profile photo is stored or loaded.

A task's `owner` is a name (the Google name it was claimed with, or text from the
tracker), so `Profiles` finds a chip by that name (or by an email, which `owned_by`
accepts too). Every member edits only their own profile: the row is the signed-in
person's, never taken from the form. Nothing here calls the model.
"""

import logging
import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, select

import db
import shell
from auth import CurrentUser, User
from models import MemberProfile

log = logging.getLogger("crush_coach")
templates = shell.templates()
router = APIRouter()

# The fixed list, in the order the picker shows them. components/icons.html draws each one.
ICONS = (
    "submarine",
    "drill",
    "octopus",
    "claw",
    "torpedo",
    "wrench",
    "anchor",
    "gear",
    "fish",
)
DEFAULT_ICON = "gear"
MAX_NAME = 20

DB_ERROR = "Couldn't reach the database right now. Nothing was saved. Try again."

# An owner cell can hold more than one person: "Sam Example, Riley Sample", "A & B", "A and B".
_OWNER_SEPARATORS = re.compile(r"\s*(?:,|;|&|\+)\s*|\s+and\s+", re.IGNORECASE)


@dataclass(frozen=True)
class Chip:
    """One person's icon and the short name shown beside it."""

    icon: str
    name: str


class ProfileError(Exception):
    """A profile that can't be saved. The message is safe to show."""


def first_name(full_name: str) -> str:
    """The first word of a name ("Riley Sample" -> "Riley"); "" for an empty one."""
    words = full_name.split()
    return words[0] if words else ""


def owner_names(owner: str | None) -> list[str]:
    """The people in an owner cell, in order, without duplicates or empty parts."""
    names: list[str] = []
    for part in _OWNER_SEPARATORS.split(owner or ""):
        name = " ".join(part.split())
        if name and name.casefold() not in {n.casefold() for n in names}:
            names.append(name)
    return names


def chip_label(chips: list[Chip]) -> str:
    """"Riley", or "Riley +1" when two people own the task."""
    if not chips:
        return ""
    return chips[0].name + (f" +{len(chips) - 1}" if len(chips) > 1 else "")


def clean_name(raw: str) -> str:
    """The short name as it will be saved: spaces tidied, at most MAX_NAME characters.
    Raises ProfileError otherwise (the form's maxlength is only a courtesy)."""
    name = " ".join((raw or "").split())
    if len(name) > MAX_NAME:
        raise ProfileError(f"Use {MAX_NAME} characters or fewer for the name.")
    if not name.isprintable():
        raise ProfileError("The name can only have letters, numbers and punctuation.")
    return name


def clean_icon(raw: str) -> str:
    if raw not in ICONS:
        raise ProfileError("Pick one of the nine icons.")
    return raw


class Profiles:
    """Every picked profile, looked up by the name or email a task's owner can hold."""

    def __init__(self, profiles: list[MemberProfile] | None = None):
        self._by_key: dict[str, MemberProfile] = {}
        for profile in profiles or []:
            if profile.full_name:
                self._by_key[profile.full_name.strip().casefold()] = profile
            self._by_key[profile.email.strip().casefold()] = profile

    def chip(self, who: str) -> Chip:
        """The chip for one owner name (or email): their pick, else first name and gear."""
        profile = self._by_key.get(who.strip().casefold())
        if profile is None:
            return Chip(DEFAULT_ICON, first_name(who) or who)
        icon = profile.icon if profile.icon in ICONS else DEFAULT_ICON
        # Matched by email, "who" is an address: the first name comes from the Google name.
        return Chip(icon, profile.display_name or first_name(profile.full_name or who) or who)

    def chips(self, owner: str | None) -> list[Chip]:
        """One chip per person in the task's owner; none for an unclaimed task."""
        return [self.chip(name) for name in owner_names(owner)]

    def mine(self, user: User) -> Chip:
        """The signed-in person's own chip."""
        profile = self._by_key.get(user["email"].strip().casefold())
        if profile is None:
            return Chip(DEFAULT_ICON, first_name(user["name"]) or user["name"])
        return self.chip(user["email"])  # the profile has their name, so the email isn't shown


def load(session: Session) -> Profiles:
    """All profiles. A database failure gives an empty set: owners then show their first
    name and the gear, and the page they are on still works."""
    try:
        return Profiles(list(session.exec(select(MemberProfile)).all()))
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("Could not load member icons (%s)", type(exc).__name__)
        return Profiles()


def save(session: Session, user: User, icon: str, display_name: str) -> MemberProfile:
    """Save the signed-in member's own profile (made on the first save). Raises
    ProfileError for an icon outside the list or a name that is too long. Commits."""
    icon = clean_icon(icon)
    name = clean_name(display_name)
    profile = session.get(MemberProfile, user["email"])
    if profile is None:
        profile = MemberProfile(email=user["email"], icon=icon)
    profile.icon = icon
    profile.display_name = name
    profile.full_name = user["name"]  # how this person's tasks find the chip
    session.add(profile)
    session.commit()
    return profile


# --- /profile ---


def render_profile(
    request: Request,
    session: Session,
    user: User,
    *,
    icon: str | None = None,
    name: str | None = None,
    error: str | None = None,
    saved: bool = False,
    status: int = 200,
) -> HTMLResponse:
    """The page. `icon` and `name` refill the form after a mistake; otherwise it shows what
    is saved (or the defaults)."""
    try:
        row = session.get(MemberProfile, user["email"])
    except Exception as exc:  # noqa: BLE001  (the form still works with the defaults)
        session.rollback()
        log.error("Could not load a member icon (%s)", type(exc).__name__)
        row = None
    chosen = icon if icon in ICONS else (row.icon if row and row.icon in ICONS else DEFAULT_ICON)
    shown_name = name if name is not None else (row.display_name if row else "")
    context = {
        "user": user,
        "icons": ICONS,
        "icon": chosen,
        "name": shown_name,
        "first_name": first_name(user["name"]) or user["name"],
        "max_name": MAX_NAME,
        "error": error,
        "saved": saved,
    }
    return templates.TemplateResponse(request, "profile.html", context, status_code=status)


@router.get("/profile", response_class=HTMLResponse)
def profile_page(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    saved: bool = False,
):
    return render_profile(request, session, user, saved=saved)


@router.post("/profile", response_class=Response)
def save_profile(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: CurrentUser,
    icon: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
):
    """Save the signed-in member's icon and name. There is no email or id in the form:
    the profile saved is always the signed-in person's own."""
    try:
        save(session, user, icon, display_name)
    except ProfileError as exc:
        return render_profile(
            request, session, user, icon=icon, name=display_name, error=str(exc), status=400
        )
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        session.rollback()
        log.error("Could not save a member icon (%s)", type(exc).__name__)
        return render_profile(
            request, session, user, icon=icon, name=display_name, error=DB_ERROR, status=503
        )
    return RedirectResponse("/profile?saved=1", status_code=303)
