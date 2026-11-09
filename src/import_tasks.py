"""Import tasks from a CSV file: parsing, guessing, and saving.

Two layouts are understood:

* The team's real tracker, a Google Sheets CSV export of the "Mechanical Tasks"
  or "Mission Actuators" tab. Title and notes rows sit above the header row,
  which starts with `#`. It has no subsystem or task type column, so those are
  guessed from the title for a lead to correct (see `/admin/import`).
* The simple sample layout (`sample_data/tasks.csv`) with explicit columns.

The web page `/admin/import` (src/admin.py) previews the parsed rows and calls
`save_rows` only after a lead confirms. The command line still works for the
sample layout:

    ../.venv/bin/python -m import_tasks sample_data/tasks.csv

Bad rows are reported by line number and skipped; good rows still import.
A task code that is already in the database is skipped, never overwritten, so
running the import twice is safe and a lead's edits are not clobbered.
"""

import csv
import io
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

import db
from models import STATUSES, SUBSYSTEMS, TASK_TYPES, Task

REQUIRED_COLUMNS = ("code", "title", "subsystem", "task_type", "stage")
COLUMNS = (*REQUIRED_COLUMNS, "owner", "status")
# Free-text Task fields that only the real tracker fills in.
DETAIL_FIELDS = (
    "description",
    "dependencies",
    "exploratory_questions",
    "skills",
    "requirements",
    "people_to_ask",
    "documentation",
    "due_date",
)

MAX_BYTES = 1_000_000
MAX_FIELD_CHARS = 5000
# Where a task starts, per type (CLAUDE.md: the first stage of each flow).
FIRST_STAGE = {"Fabricate": "drawing", "Design": "requirements", "Validate": "test plan"}

FORMAT_TRACKER = "tracker"
FORMAT_SAMPLE = "sample"


class ImportFileError(Exception):
    """The CSV file itself is unusable (missing, unreadable, wrong columns)."""


@dataclass
class ParsedRow:
    line: int  # line in the file where the row starts
    code: str
    title: str
    subsystem: str | None = None
    task_type: str | None = None
    stage: str = ""  # blank: start at the first stage of the task type
    owner: str | None = None
    status: str = "todo"
    details: dict[str, str | None] = field(
        default_factory=lambda: dict.fromkeys(DETAIL_FIELDS)
    )
    error: str | None = None


@dataclass
class ParsedFile:
    format: str
    rows: list[ParsedRow]


@dataclass
class ImportResult:
    added: int = 0
    skipped_existing: int = 0
    errors: list[str] = field(default_factory=list)
    added_codes: list[str] = field(default_factory=list)
    skipped_codes: list[str] = field(default_factory=list)


# --- guessing (a lead corrects these in the preview) ---

_SUBSYSTEM_WORDS = (
    ("Torpedo", r"torpedo"),
    ("Gripper", r"grip|claw|jaw"),
    ("Thrusters", r"thruster|motor|propell"),
    ("Seals/Waterproofing", r"seal|gasket|o-ring|waterproof|potting|leak"),
    ("Electronics Mounts", r"mount|tray|standoff|board|pcb|electronics"),
    ("Enclosure", r"enclosure|panel|lid|wall|hull|housing|flange|end ?cap"),
)
_CODE_PREFIX_SUBSYSTEM = {
    "E": "Electronics Mounts",
    "G": "Gripper",
    "T": "Torpedo",
}
_VALIDATE_WORDS = re.compile(r"\b(?:test|validat|verif|calculat)", re.IGNORECASE)
_DESIGN_WORDS = re.compile(
    r"\b(?:design|model|cad|sketch|select|choose|concept)", re.IGNORECASE
)


def guess_subsystem(title: str, code: str = "") -> str | None:
    """Best guess from title keywords, then the code's letter. None if unsure."""
    for subsystem, words in _SUBSYSTEM_WORDS:
        if re.search(rf"\b(?:{words})", title, re.IGNORECASE):
            return subsystem
    return _CODE_PREFIX_SUBSYSTEM.get(code[:1].upper())


def guess_task_type(title: str) -> str:
    if _VALIDATE_WORDS.search(title):
        return "Validate"
    if _DESIGN_WORDS.search(title):
        return "Design"
    return "Fabricate"


# --- reading ---


def decode_csv_bytes(data: bytes) -> str:
    """Bytes from an upload or file to text, with plain-language failures."""
    if len(data) > MAX_BYTES:
        raise ImportFileError(f"That file is too large (limit {MAX_BYTES // 1000} KB).")
    if not data.strip():
        raise ImportFileError("That file is empty.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFileError(
            "That file is not valid UTF-8 text. Export it from Google Sheets "
            "as CSV and upload that."
        ) from exc


def _normalize_header(cell: str) -> str:
    return " ".join(cell.split()).casefold()


# Real-tracker header -> field. Matched by prefix because Sheets headers carry
# extra words and line breaks; "Relavent" is the tracker's own spelling.
_TRACKER_HEADERS = (
    ("#", "code"),
    ("task", "title"),
    ("owner", "owner"),
    ("detailed description", "description"),
    ("dependencies", "dependencies"),
    ("exploratory questions", "exploratory_questions"),
    ("skills", "skills"),
    ("requirements", "requirements"),
    ("people to ask", "people_to_ask"),
    ("relavent documentation", "documentation"),
    ("relevant documentation", "documentation"),
    ("documentation", "documentation"),
    ("potential due date", "due_date"),
    ("due date", "due_date"),
)


def _tracker_field(header: str) -> str | None:
    text = _normalize_header(header)
    if text == "#":
        return "code"
    if text == "task":
        return "title"
    for prefix, name in _TRACKER_HEADERS[2:]:
        if text.startswith(prefix):
            return name
    return None


def _read_records(text: str) -> list[tuple[int, list[str]]]:
    """(starting line, cells) for each CSV record, blank records dropped."""
    reader = csv.reader(io.StringIO(text, newline=""))
    records: list[tuple[int, list[str]]] = []
    try:
        while True:
            start = reader.line_num + 1
            try:
                cells = next(reader)
            except StopIteration:
                break
            if any(cell.strip() for cell in cells):
                records.append((start, cells))
    except csv.Error as exc:
        raise ImportFileError("That file doesn't look like a CSV.") from exc
    return records


def _clean(value: str) -> str:
    return value.strip()


def _too_long(row: ParsedRow) -> str | None:
    values = {
        "code": row.code,
        "title": row.title,
        "owner": row.owner,
        "stage": row.stage,
        **row.details,
    }
    for name, value in values.items():
        if value and len(value) > MAX_FIELD_CHARS:
            return f"{name} is longer than {MAX_FIELD_CHARS} characters"
    return None


def parse_tracker_csv(text: str) -> ParsedFile:
    """Parse either layout. Raises ImportFileError if the file is unusable."""
    records = _read_records(text)
    if not records:
        raise ImportFileError("That file is empty.")

    first_cells = [_normalize_header(c) for c in records[0][1]]
    if all(column in first_cells for column in REQUIRED_COLUMNS):
        return _parse_sample(records)

    for index, (_, cells) in enumerate(records):
        if cells and cells[0].strip() == "#":
            return _parse_tracker(records[index:])

    if any(column in first_cells for column in REQUIRED_COLUMNS):
        missing = [c for c in REQUIRED_COLUMNS if c not in first_cells]
        raise ImportFileError(f"CSV is missing column(s): {', '.join(missing)}")
    raise ImportFileError(
        "Couldn't find the header row. For a tracker export, the header row "
        "must start with “#”."
    )


def _parse_sample(records: list[tuple[int, list[str]]]) -> ParsedFile:
    header = [_normalize_header(c) for c in records[0][1]]
    rows: list[ParsedRow] = []
    seen: dict[str, int] = {}
    for line, cells in records[1:]:
        values = {
            column: _clean(cells[header.index(column)])
            if column in header and header.index(column) < len(cells)
            else ""
            for column in COLUMNS
        }
        row = ParsedRow(
            line=line,
            code=values["code"],
            title=values["title"],
            subsystem=values["subsystem"],
            task_type=values["task_type"],
            stage=values["stage"],
            owner=values["owner"] or None,
            status=values["status"] or "todo",
        )
        row.error = _sample_problem(row) or _duplicate_problem(row, seen)
        rows.append(row)
    return ParsedFile(FORMAT_SAMPLE, rows)


def _sample_problem(row: ParsedRow) -> str | None:
    for column in ("code", "title", "stage"):
        if not getattr(row, column):
            return f"{column} is empty"
    if row.subsystem not in SUBSYSTEMS:
        return f"unknown subsystem {row.subsystem!r}"
    if row.task_type not in TASK_TYPES:
        return f"unknown task type {row.task_type!r}"
    if row.status not in STATUSES:
        return f"unknown status {row.status!r}"
    return _too_long(row)


def _duplicate_problem(row: ParsedRow, seen: dict[str, int]) -> str | None:
    """Flag a code that an earlier good row in this file already uses."""
    if row.error:
        return row.error
    if row.code in seen:
        return f"duplicate code {row.code} in this file (first on line {seen[row.code]})"
    seen[row.code] = row.line
    return None


def _parse_tracker(records: list[tuple[int, list[str]]]) -> ParsedFile:
    fields: dict[str, int] = {}
    for column, cell in enumerate(records[0][1]):
        name = _tracker_field(cell)
        if name and name not in fields:
            fields[name] = column
    if "title" not in fields:
        raise ImportFileError("CSV is missing column(s): Task")

    def cell(cells: list[str], name: str) -> str:
        column = fields.get(name)
        return _clean(cells[column]) if column is not None and column < len(cells) else ""

    rows: list[ParsedRow] = []
    seen: dict[str, int] = {}
    for line, cells in records[1:]:
        row = ParsedRow(
            line=line,
            code=cell(cells, "code"),
            title=cell(cells, "title"),
            owner=cell(cells, "owner") or None,
            details={name: cell(cells, name) or None for name in DETAIL_FIELDS},
        )
        row.subsystem = guess_subsystem(row.title, row.code)
        row.task_type = guess_task_type(row.title)
        problem = None
        if not row.code:
            problem = "code is empty"
        elif not row.title:
            problem = "title is empty"
        row.error = _duplicate_problem(row, seen) if not problem else problem
        row.error = row.error or _too_long(row)
        rows.append(row)
    return ParsedFile(FORMAT_TRACKER, rows)


# --- saving ---


def _save_problem(row: ParsedRow) -> str | None:
    """Everything that must hold before a row becomes a Task."""
    if row.error:
        return row.error
    if not row.code or not row.title:
        return "code or title is empty"
    if row.subsystem not in SUBSYSTEMS:
        return "no subsystem chosen" if not row.subsystem else (
            f"unknown subsystem {row.subsystem!r}"
        )
    if row.task_type not in TASK_TYPES:
        return f"unknown task type {row.task_type!r}"
    if row.status not in STATUSES:
        return f"unknown status {row.status!r}"
    return _too_long(row)


def save_rows(session: Session, rows: list[ParsedRow]) -> ImportResult:
    """Add new tasks. Existing codes are skipped, never overwritten."""
    result = ImportResult()
    existing = set(session.exec(select(Task.code)).all())
    for row in rows:
        problem = _save_problem(row)
        if problem:
            result.errors.append(f"line {row.line}: {problem}")
        elif row.code in existing:
            result.skipped_existing += 1
            result.skipped_codes.append(row.code)
        else:
            assert row.subsystem and row.task_type  # checked in _save_problem
            session.add(
                Task(
                    code=row.code,
                    title=row.title,
                    subsystem=row.subsystem,
                    task_type=row.task_type,
                    stage=row.stage or FIRST_STAGE[row.task_type],
                    owner=row.owner,
                    status=row.status,
                    **row.details,
                )
            )
            existing.add(row.code)  # a repeat later in the list is then skipped
            result.added += 1
            result.added_codes.append(row.code)
    session.commit()
    return result


def read_rows(path: Path) -> ParsedFile:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImportFileError(f"Could not read {path} ({type(exc).__name__})") from exc
    return parse_tracker_csv(decode_csv_bytes(data))


def import_tasks(session: Session, path: Path) -> ImportResult:
    parsed = read_rows(path)
    if parsed.format == FORMAT_TRACKER:
        raise ImportFileError(
            "This is a tracker export. A lead imports it at /admin/import, "
            "where the guessed subsystems can be checked first."
        )
    return save_rows(session, parsed.rows)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage: python -m import_tasks <tasks.csv>", file=sys.stderr)
        return 1
    path = Path(args[0])

    try:
        db.run_migrations()
        with Session(db.engine) as session:
            result = import_tasks(session, path)
    except ImportFileError as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001  (never print the message: it can hold the URL)
        print(f"Import failed: database error ({type(exc).__name__})", file=sys.stderr)
        return 1

    print(
        f"{result.added} added, {result.skipped_existing} already present, "
        f"{len(result.errors)} rejected."
    )
    for message in result.errors:
        print(f"  rejected {message}", file=sys.stderr)
    return 2 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
