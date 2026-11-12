"""What the Drive index leaves out: noise folders, file types and file names.

The rules live in config, not code: `drive_index_exclude.txt` (committed, one rule
per line) plus the optional `DRIVE_INDEX_EXCLUDE` environment variable (rules
separated by newlines or semicolons), so a team lead can change them on Railway
without a deploy. Matching is case-insensitive; `*` and `?` are wildcards.

    .git                a folder with this name, anywhere, and everything in it
    Pool Runs/Raw       a folder "Raw" directly inside a folder "Pool Runs"
    file: *.log         a file whose name matches
    mime: image/*       a file whose Google/Drive MIME type matches

An excluded item is not indexed at all: no row, no name, no text. A full sync
also removes rows that a new rule now excludes; an incremental sync only applies
rules to what changed.
"""

import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

log = logging.getLogger("crush_coach")

ENV_VAR = "DRIVE_INDEX_EXCLUDE"
DEFAULT_FILE = Path(__file__).parent / "drive_index_exclude.txt"


@dataclass(frozen=True)
class Rule:
    text: str  # as written, for reports
    kind: str  # "folder", "file" or "mime"
    parts: tuple[str, ...]  # folder: the path segments; file/mime: the one pattern


class ExcludeRules:
    """A parsed rule list. Falsy when empty, so callers can skip the work."""

    def __init__(self, rules: Iterable[Rule] = ()):
        self.rules = tuple(rules)

    def __bool__(self) -> bool:
        return bool(self.rules)

    def __len__(self) -> int:
        return len(self.rules)

    def match(
        self,
        folders: Sequence[str],
        name: str,
        mime_type: str,
        is_folder: bool,
    ) -> Rule | None:
        """The first rule that excludes this item, or None.

        `folders` are the names of the folders above the item, outermost first,
        not counting the shared drive's own name.
        """
        chain = [part.casefold() for part in folders]
        if is_folder:
            chain.append(name.casefold())
        for rule in self.rules:
            if rule.kind == "folder":
                if _contains_run(chain, rule.parts):
                    return rule
            elif is_folder:
                continue  # file and mime rules never exclude a folder itself
            elif rule.kind == "file" and fnmatchcase(name.casefold(), rule.parts[0]) or rule.kind == "mime" and fnmatchcase(
                mime_type.casefold(), rule.parts[0]
            ):
                return rule
        return None


def _contains_run(chain: list[str], parts: tuple[str, ...]) -> bool:
    """Whether the patterns match consecutive entries somewhere in `chain`."""
    for start in range(len(chain) - len(parts) + 1):
        if all(
            fnmatchcase(chain[start + i], pattern) for i, pattern in enumerate(parts)
        ):
            return True
    return False


def parse_rules(lines: Iterable[str]) -> ExcludeRules:
    """Rules from text lines. Blank lines and # comments are ignored; a line that
    can't be understood is skipped with a warning, never fatal."""
    rules: list[Rule] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        prefix, colon, rest = line.partition(":")
        if colon and prefix.strip().casefold() in ("file", "mime"):
            kind, body = prefix.strip().casefold(), rest.strip()
            parts = (body.casefold(),) if body else ()
        else:
            kind = "folder"
            parts = tuple(
                segment.strip().casefold()
                for segment in line.replace("\\", "/").split("/")
                if segment.strip()
            )
        if not parts:
            log.warning("Drive index: ignoring an empty exclude rule: %r", line)
            continue
        rules.append(Rule(line, kind, parts))
    return ExcludeRules(rules)


def load_rules(
    path: Path | None = DEFAULT_FILE, environ: dict[str, str] | None = None
) -> ExcludeRules:
    """The committed file's rules plus any in DRIVE_INDEX_EXCLUDE.

    A missing or unreadable file is a warning, not a failure: syncing without
    exclusions is better than not syncing.
    """
    environ = os.environ if environ is None else environ
    lines: list[str] = []
    if path is not None:
        try:
            lines.extend(path.read_text(encoding="utf-8").splitlines())
        except OSError as exc:
            log.warning(
                "Drive index: could not read %s (%s); using no file rules",
                path.name,
                type(exc).__name__,
            )
    extra = environ.get(ENV_VAR, "")
    lines.extend(part for chunk in extra.splitlines() for part in chunk.split(";"))
    return parse_rules(lines)
