"""Which Drive results rank lower: obsolete or old folders, Alfie, and old seasons.

Like the exclude rules (drive_exclude.py), this is config, not code:
`drive_index_demote.txt` (committed) plus the optional `DRIVE_INDEX_DEMOTE`
environment variable (lines separated by newlines or semicolons), so a lead can
change it on Railway without a deploy. Demoted files still show up, further down:
the team's old documents are sometimes the only ones that answer a question.

    OBSOLETE            a folder path containing this word (whole words, any case)
    for Comparison      ...or this phrase; * matches word characters (Archive*)
    season_start_month = 8        a season (folders named 2024-2025) starts in August
    old_after_seasons = 1         a season more than 1 season behind the current is old
"""

import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

log = logging.getLogger("crush_coach")

ENV_VAR = "DRIVE_INDEX_DEMOTE"
DEFAULT_FILE = Path(__file__).parent / "drive_index_demote.txt"

# "2024-2025", "2024/2025", "2024 - 25": a season folder or file name.
_SEASON = re.compile(r"(?<!\d)(20\d{2})\s*[-–/]\s*(20\d{2}|\d{2})(?!\d)")


@dataclass(frozen=True)
class Assessment:
    """Why a file counts as old: words in its folder path, and/or an old season."""

    folder_words: tuple[str, ...] = ()
    old_season: int | None = None  # the year that season started

    @property
    def reasons(self) -> tuple[str, ...]:
        """The reasons in words a person (or the model) can read."""
        found = [f"folder path mentions {word}" for word in self.folder_words]
        if self.old_season is not None:
            found.append(f"from the {self.old_season}-{self.old_season + 1} season")
        return tuple(found)


@dataclass(frozen=True)
class DemoteRules:
    phrases: tuple[tuple[str, "re.Pattern[str]"], ...] = ()
    season_start_month: int = 8
    old_after_seasons: int = 1

    @classmethod
    def none(cls) -> "DemoteRules":
        """Rules that demote nothing: no phrases, and no season is ever old."""
        return cls(old_after_seasons=10_000)

    def current_season(self, today: date) -> int:
        """The year the current season started (2026 for the 2026-2027 season)."""
        return today.year if today.month >= self.season_start_month else today.year - 1

    def season_of(self, folder_path: str, modified: datetime | None) -> int | None:
        """The season a file belongs to: its season folder, else its modified date."""
        for match in _SEASON.finditer(folder_path):
            start, end = int(match.group(1)), match.group(2)
            end_year = int(end) if len(end) == 4 else (start // 100) * 100 + int(end)
            if end_year == start + 1:
                return start
        if modified is not None:
            if modified.month >= self.season_start_month:
                return modified.year
            return modified.year - 1
        return None

    def assess(
        self,
        folder_path: str,
        modified: datetime | None,
        today: date | None = None,
    ) -> Assessment:
        """Whether this file counts as old, and why."""
        today = today or datetime.now(UTC).date()
        words = tuple(
            text for text, pattern in self.phrases if pattern.search(folder_path)
        )
        season = self.season_of(folder_path, modified)
        old_season = None
        if season is not None and self.current_season(today) - season > (
            self.old_after_seasons
        ):
            old_season = season
        return Assessment(words, old_season)


def _phrase_pattern(text: str) -> "re.Pattern[str]":
    """Whole words, any case; * matches letters and digits.

    'Old' is not 'Holder', but it is 'Old Crush' and 'old_notes': an underscore
    separates words in file and folder names.
    """
    body = r"[\W_]+".join(
        "[^\\W_]*".join(re.escape(part) for part in word.split("*"))
        for word in text.split()
    )
    return re.compile(rf"(?<![^\W_]){body}(?![^\W_])", re.IGNORECASE)


def parse_rules(lines: Iterable[str]) -> DemoteRules:
    """Rules from text lines; blank lines and # comments are ignored, and a line
    that can't be understood is skipped with a warning, never fatal."""
    phrases: list[tuple[str, re.Pattern[str]]] = []
    settings: dict[str, int] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, equals, value = line.partition("=")
        if equals and key.strip() in ("season_start_month", "old_after_seasons"):
            try:
                settings[key.strip()] = int(value.strip())
            except ValueError:
                log.warning("Drive index: ignoring a bad setting: %r", line)
            continue
        phrases.append((line, _phrase_pattern(line)))
    month = settings.get("season_start_month", DemoteRules.season_start_month)
    if not 1 <= month <= 12:
        log.warning("Drive index: season_start_month must be 1-12; using the default")
        month = DemoteRules.season_start_month
    seasons = max(settings.get("old_after_seasons", DemoteRules.old_after_seasons), 0)
    return DemoteRules(tuple(phrases), month, seasons)


def load_rules(
    path: Path | None = DEFAULT_FILE, environ: dict[str, str] | None = None
) -> DemoteRules:
    """The committed file's rules plus any in DRIVE_INDEX_DEMOTE.

    A missing or unreadable file is a warning, not a failure: ranking without the
    list is better than not searching.
    """
    environ = os.environ if environ is None else environ
    lines: list[str] = []
    if path is not None:
        try:
            lines.extend(path.read_text(encoding="utf-8").splitlines())
        except OSError as exc:
            log.warning(
                "Drive index: could not read %s (%s); no demotion words",
                path.name,
                type(exc).__name__,
            )
    extra = environ.get(ENV_VAR, "")
    lines.extend(part for chunk in extra.splitlines() for part in chunk.split(";"))
    return parse_rules(lines)
