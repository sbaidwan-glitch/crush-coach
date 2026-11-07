"""One-time import of tasks from a CSV file.

Run from src/:

    ../.venv/bin/python -m import_tasks sample_data/tasks.csv

Bad rows are reported by line number and skipped; good rows still import.
A task code that is already in the database is skipped, never overwritten, so
running the import twice is safe and a lead's edits are not clobbered.
"""

import csv
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

import db
from models import STATUSES, SUBSYSTEMS, TASK_TYPES, Task

REQUIRED_COLUMNS = ("code", "title", "subsystem", "task_type", "stage")
COLUMNS = (*REQUIRED_COLUMNS, "owner", "status")


class ImportFileError(Exception):
    """The CSV file itself is unusable (missing, unreadable, wrong columns)."""


@dataclass
class ImportResult:
    added: int = 0
    skipped_existing: int = 0
    errors: list[str] = field(default_factory=list)


def read_rows(path: Path) -> tuple[list[Task], list[str]]:
    """Parse and validate the CSV. Returns (valid tasks, error messages)."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = [
                c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])
            ]
            if missing:
                raise ImportFileError(f"CSV is missing column(s): {', '.join(missing)}")
            rows = list(reader)
    except OSError as exc:
        raise ImportFileError(f"Could not read {path} ({type(exc).__name__})") from exc
    except UnicodeDecodeError as exc:
        raise ImportFileError(f"{path} is not valid UTF-8 text") from exc

    tasks: list[Task] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):  # line 1 is the header
        values = {c: (row.get(c) or "").strip() for c in COLUMNS}
        problem = _problem(values, seen)
        if problem:
            errors.append(f"line {line}: {problem}")
            continue
        seen.add(values["code"])
        tasks.append(
            Task(
                code=values["code"],
                title=values["title"],
                subsystem=values["subsystem"],
                task_type=values["task_type"],
                stage=values["stage"],
                owner=values["owner"] or None,
                status=values["status"] or "todo",
            )
        )
    return tasks, errors


def _problem(values: dict[str, str], seen: set[str]) -> str | None:
    for column in ("code", "title", "stage"):
        if not values[column]:
            return f"{column} is empty"
    if values["code"] in seen:
        return f"duplicate code {values['code']} in this file"
    if values["subsystem"] not in SUBSYSTEMS:
        return f"unknown subsystem {values['subsystem']!r}"
    if values["task_type"] not in TASK_TYPES:
        return f"unknown task type {values['task_type']!r}"
    if values["status"] and values["status"] not in STATUSES:
        return f"unknown status {values['status']!r}"
    return None


def import_tasks(session: Session, path: Path) -> ImportResult:
    tasks, errors = read_rows(path)
    result = ImportResult(errors=errors)
    existing = set(session.exec(select(Task.code)).all())
    for task in tasks:
        if task.code in existing:
            result.skipped_existing += 1
        else:
            session.add(task)
            result.added += 1
    session.commit()
    return result


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
