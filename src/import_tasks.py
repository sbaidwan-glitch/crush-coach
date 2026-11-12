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
On the command line, a task code that is already in the database is skipped, never
overwritten, so running the import twice is safe.

On the web page, a lead can re-import a newer export: a row whose code already exists
is compared with the task, and the fields that differ are shown in the review and
updated on Import (see `update_fields_for`). Stage and status are never touched: they
move as people work.
"""

import csv
import io
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

import db
from models import STATUSES, SUBSYSTEMS, TASK_DETAIL_LABELS, TASK_TYPES, Task

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
MAX_CODE_CHARS = 40
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


@dataclass
class ParsedFile:
    format: str
    rows: list[ParsedRow]


@dataclass
class ImportResult:
    added: int = 0
    updated: int = 0
    skipped_existing: int = 0
    errors: list[str] = field(default_factory=list)
    added_codes: list[str] = field(default_factory=list)
    skipped_codes: list[str] = field(default_factory=list)
    # code -> labels of the fields that were changed
    updated_fields: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Problem:
    """Something wrong with one row, worded for the person who has to fix it."""

    field: str  # code, title, subsystem, task_type, or row (not fixable in place)
    message: str
    suggestion: str | None = None  # a free code to offer when the field is "code"


@dataclass
class Change:
    """One field of an existing task that the file would change."""

    name: str  # the Task attribute, e.g. "owner"
    label: str  # as shown to the lead, e.g. "Owner"
    old: str  # "" when empty
    new: str


@dataclass
class RowCheck:
    state: str  # add, update, exists (nothing to change), fix, or left_out
    problems: list[Problem] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)  # only for "update"

    def problem(self, name: str) -> Problem | None:
        return next((p for p in self.problems if p.field == name), None)


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


def _too_long_field(row: ParsedRow, fields: Sequence[str] | None = None) -> str | None:
    """Label of the first free-text field over the length limit, if any.

    `fields` limits the check to those a re-import would write (None: all of them).
    """
    values = {
        "owner": ("Owner", row.owner),
        "stage": ("Stage", row.stage),
        **{name: (TASK_DETAIL_LABELS[name], row.details.get(name)) for name in DETAIL_FIELDS},
    }
    for name, (label, value) in values.items():
        if (fields is None or name in fields) and value and len(value) > MAX_FIELD_CHARS:
            return label
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
    for line, cells in records[1:]:
        values = {
            column: _clean(cells[header.index(column)])
            if column in header and header.index(column) < len(cells)
            else ""
            for column in COLUMNS
        }
        rows.append(
            ParsedRow(
                line=line,
                code=values["code"],
                title=values["title"],
                subsystem=values["subsystem"],
                task_type=values["task_type"],
                stage=values["stage"],
                owner=values["owner"] or None,
                status=values["status"] or "todo",
            )
        )
    return ParsedFile(FORMAT_SAMPLE, rows)


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
        rows.append(row)
    return ParsedFile(FORMAT_TRACKER, rows)


# --- checking and saving ---

_CODE_PATTERN = re.compile(r"^([A-Za-z]+)(\d+)")


def next_free_code(code: str, taken: set[str]) -> str | None:
    """The next unused code with the same letters as `code` (M22(2) -> M25).

    A blank or unrecognisable code borrows the most common letters in `taken`.
    """
    match = _CODE_PATTERN.match(code)
    if match:
        prefix, width = match.group(1), len(match.group(2))
    else:
        prefixes = Counter(
            m.group(1) for c in taken if (m := _CODE_PATTERN.match(c))
        )
        if not prefixes:
            return None
        prefix, width = prefixes.most_common(1)[0][0], 2
    numbers = [
        int(m.group(2))
        for c in taken
        if (m := _CODE_PATTERN.match(c)) and m.group(1) == prefix
    ]
    return f"{prefix}{max(numbers, default=0) + 1:0{width}d}"


FIELD_LABELS = {
    "title": "Title",
    "owner": "Owner",
    "subsystem": "Subsystem",
    "task_type": "Type",
    **TASK_DETAIL_LABELS,
}


def update_fields_for(is_tracker: bool) -> tuple[str, ...]:
    """The fields a re-import may change on an existing task, by file layout.

    Only what the file really says. The tracker has no subsystem or type column
    (they are guesses a lead corrected), and the sample layout has no detail columns,
    so those would otherwise be wiped. Stage and status are never updated: they move
    as people work, and an old export must not set a task back.
    """
    if is_tracker:
        return ("title", "owner", *DETAIL_FIELDS)
    return ("title", "subsystem", "task_type", "owner")


def _field_value(source: ParsedRow | Task, name: str) -> str:
    if isinstance(source, ParsedRow) and name in DETAIL_FIELDS:
        value = source.details.get(name)
    else:
        value = getattr(source, name, None)
    return (value or "").strip()


def changes_for(task: Task, row: ParsedRow, fields: Sequence[str]) -> list[Change]:
    """The fields in `fields` where the row differs from the stored task.

    Empty and missing are the same thing; a cell cleared in the sheet is a change.
    """
    changes = []
    for name in fields:
        old, new = _field_value(task, name), _field_value(row, name)
        if old != new:
            changes.append(Change(name, FIELD_LABELS[name], old, new))
    return changes


def _row_problems(row: ParsedRow, fields: Sequence[str] | None = None) -> list[Problem]:
    """What is wrong with the row's values. `fields` limits this to those a re-import
    would write (None: a new task, everything is checked)."""

    def applies(name: str) -> bool:
        return fields is None or name in fields

    problems: list[Problem] = []
    if applies("title"):
        if not row.title:
            problems.append(Problem("title", "Add a title."))
        elif len(row.title) > MAX_FIELD_CHARS:
            problems.append(Problem("title", "Shorten this title."))
    if applies("subsystem"):
        if not row.subsystem:
            problems.append(Problem("subsystem", "Choose a subsystem."))
        elif row.subsystem not in SUBSYSTEMS:
            problems.append(
                Problem("subsystem", f"“{row.subsystem}” isn’t a subsystem. Choose one.")
            )
    if applies("task_type") and row.task_type not in TASK_TYPES:
        problems.append(Problem("task_type", "Choose a type."))
    if fields is None and row.status not in STATUSES:
        problems.append(
            Problem(
                "row",
                f"Status “{row.status}” isn’t valid (use todo, in_progress or "
                "done). Fix it in the file and upload again, or leave this row out.",
            )
        )
    too_long = _too_long_field(row, fields)
    if too_long:
        problems.append(
            Problem(
                "row",
                f"{too_long} is over {MAX_FIELD_CHARS:,} characters. Shorten it "
                "in the sheet and upload again, or leave this row out.",
            )
        )
    return problems


def check_rows(
    rows: list[ParsedRow],
    existing: Mapping[str, Task] | set[str],
    included: list[bool] | None = None,
    original_codes: list[str] | None = None,
    update_fields: Sequence[str] | None = None,
) -> list[RowCheck]:
    """What would happen to each row, and what is wrong with it.

    `included` marks rows the lead left in (default all). `original_codes` are the
    codes as parsed from the file, so a code the lead typed that collides with an
    existing task is a problem, while an unedited existing code is not.

    An unedited existing code is skipped ("exists"), unless `existing` maps codes to
    their tasks and `update_fields` is given: then the row is compared with the task
    and is an "update" (with the `changes`) if any of those fields differ.
    """
    included = included if included is not None else [True] * len(rows)
    originals = original_codes if original_codes is not None else [r.code for r in rows]
    taken = set(existing) | {r.code for r, inc in zip(rows, included) if inc and r.code}
    seen: set[str] = set()
    checks: list[RowCheck] = []
    for row, inc, original in zip(rows, included, originals, strict=True):
        if not inc:
            checks.append(RowCheck("left_out"))
            continue
        problems: list[Problem] = []
        code = row.code

        def offer(for_code: str) -> str | None:
            free = next_free_code(for_code, taken)
            if free:
                taken.add(free)  # never offer the same code to two rows
            return free

        if not code:
            problems.append(Problem("code", "Add a code for this row.", offer("")))
        elif len(code) > MAX_CODE_CHARS:
            problems.append(
                Problem("code", f"Use a code of {MAX_CODE_CHARS} characters or fewer.")
            )
        elif code in seen:
            problems.append(
                Problem(
                    "code",
                    f"Two rows use {code}. Give this one a new code:",
                    offer(code),
                )
            )
        elif code in existing and code != original:
            problems.append(
                Problem(
                    "code",
                    f"{code} is already in the tracker. Use a different code:",
                    offer(code),
                )
            )
        if code:
            seen.add(code)

        if not problems and code in existing:
            task = existing.get(code) if isinstance(existing, Mapping) else None
            if task is None or update_fields is None:
                # Skipped whatever else is wrong with it: nothing will be written.
                checks.append(RowCheck("exists"))
                continue
            changes = changes_for(task, row, update_fields)
            if not changes:
                checks.append(RowCheck("exists"))
                continue
            problems = _row_problems(row, [c.name for c in changes])
            checks.append(
                RowCheck("fix", problems) if problems else RowCheck("update", changes=changes)
            )
            continue

        problems += _row_problems(row)
        checks.append(RowCheck("fix", problems) if problems else RowCheck("add"))
    return checks


def save_rows(
    session: Session,
    rows: list[ParsedRow],
    original_codes: list[str] | None = None,
    update_fields: Sequence[str] | None = None,
) -> ImportResult:
    """Add new tasks.

    An existing code is skipped, never overwritten, unless `update_fields` is given:
    then those fields of the existing task are brought in line with the row.
    Rows that fail `check_rows` are reported in `errors` and not saved.
    """
    result = ImportResult()
    existing = {t.code: t for t in session.exec(select(Task)).all()}
    checks = check_rows(rows, existing, None, original_codes, update_fields)
    for row, check in zip(rows, checks, strict=True):
        if check.state == "fix":
            messages = " ".join(p.message.rstrip(":") for p in check.problems)
            result.errors.append(f"line {row.line}: {messages}")
        elif check.state == "exists":
            result.skipped_existing += 1
            result.skipped_codes.append(row.code)
        elif check.state == "update":
            task = existing[row.code]
            for change in check.changes:
                # An emptied cell clears the field; the title can't be empty (checked).
                setattr(task, change.name, change.new or None)
            session.add(task)
            result.updated += 1
            result.updated_fields[row.code] = [c.label for c in check.changes]
        else:
            assert row.subsystem and row.task_type  # checked by check_rows
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
