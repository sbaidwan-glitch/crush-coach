"""The team's local day: one place for "today" and which date a moment falls on.

Timestamps are stored as naive UTC (`models.utc_now`). People read days in the team's
time zone (`COACH_TIMEZONE`, default America/Los_Angeles): the coach's daily cap, the
work log ("hasn't logged today") and the date headings written to the Crush document
all use the same boundary.
"""

import os
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/Los_Angeles"
# Fixed, not strftime("%b"): the heading is matched against text in the Crush document, so
# it must not change with the server's locale.
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def timezone() -> tzinfo:
    try:
        return ZoneInfo(os.environ.get("COACH_TIMEZONE") or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001  (bad name or no tz database: fall back to UTC)
        return UTC


def local_date(moment: datetime) -> date:
    """The local calendar day a stored (naive UTC) moment falls on."""
    return moment.replace(tzinfo=UTC).astimezone(timezone()).date()


def today() -> date:
    return datetime.now(timezone()).date()


def day_start_utc() -> datetime:
    """Start of the current local day, as naive UTC (how created_at is stored)."""
    local_now = datetime.now(timezone())
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).replace(tzinfo=None)


def week_start_utc() -> datetime:
    """Start of the current local week (Monday 00:00), as naive UTC."""
    local_now = datetime.now(timezone())
    monday = local_now - timedelta(days=local_now.weekday())
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).replace(tzinfo=None)


def heading(day: date) -> str:
    """A day as the Record and the Crush document write it: "17 Nov 2026"."""
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"
