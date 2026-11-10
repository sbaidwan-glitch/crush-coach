"""Lead-only pages. `/admin/import` loads tasks from the team's tracker CSV.

Flow: upload -> review (nothing saved) -> Import. The review page is stateless:
the rows exactly as parsed travel in a hidden field, and the lead's edits (codes,
titles, subsystems, types, which rows to include) travel as form fields. Every
change re-checks all rows on the server, and Import re-checks once more, so the
browser is never trusted and nothing is stored between steps.
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.datastructures import FormData, UploadFile

import db
import import_tasks as importer
from auth import LeadUser
from models import SUBSYSTEMS, TASK_TYPES, Task

log = logging.getLogger("crush_coach")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter(prefix="/admin")

DB_ERROR = (
    "Couldn't reach the database right now. Nothing was saved. Try again in a moment."
)
STALE_PREVIEW = (
    "The preview data was missing or damaged, so nothing was saved. "
    "Upload the file again."
)


def _upload_page(request: Request, user, *, error: str | None = None, status=200):
    return templates.TemplateResponse(
        request, "admin_import.html", {"user": user, "error": error}, status_code=status
    )


def _row_from_dict(data: Any) -> importer.ParsedRow:
    """Rebuild a row from the hidden field, coercing every type."""
    if not isinstance(data, dict):
        raise TypeError("row is not an object")

    def text(value: Any) -> str | None:
        return value if isinstance(value, str) else None

    details = data.get("details")
    if not isinstance(details, dict):
        details = {}
    line = data.get("line")
    return importer.ParsedRow(
        line=line if isinstance(line, int) else 0,
        code=text(data.get("code")) or "",
        title=text(data.get("title")) or "",
        subsystem=text(data.get("subsystem")),
        task_type=text(data.get("task_type")),
        stage=text(data.get("stage")) or "",
        owner=text(data.get("owner")),
        status=text(data.get("status")) or "todo",
        details={name: text(details.get(name)) for name in importer.DETAIL_FIELDS},
    )


@dataclass
class Review:
    """One row on the review page."""

    index: int
    row: importer.ParsedRow
    original: importer.ParsedRow
    check: importer.RowCheck
    included: bool

    @property
    def code_editable(self) -> bool:
        """Keep the code box once it was a problem, so a fixed value stays visible."""
        return self.check.state != "exists" and (
            not self.original.code
            or self.row.code != self.original.code
            or self.check.problem("code") is not None
        )

    @property
    def title_editable(self) -> bool:
        return self.check.state != "exists" and (
            not self.original.title
            or self.row.title != self.original.title
            or self.check.problem("title") is not None
        )

    @property
    def code_value(self) -> str:
        """What the code box shows: the suggestion until the lead changes it."""
        problem = self.check.problem("code")
        if problem and problem.suggestion and self.row.code == self.original.code:
            return problem.suggestion
        return self.row.code


def _apply_edits(
    originals: list[importer.ParsedRow], form: FormData
) -> tuple[list[importer.ParsedRow], list[bool]]:
    """Rows as the lead has them now, and which are still included."""
    rows: list[importer.ParsedRow] = []
    included: list[bool] = []
    accepted = set(form.getlist("accept"))  # rows whose "Use this code" was clicked
    for i, original in enumerate(originals):
        row = _row_from_dict(asdict(original))  # a copy to edit
        for name in ("code", "title"):
            value = form.get(f"{name}_{i}")
            if not isinstance(value, str):
                continue
            # A suggested code the lead hasn't touched or accepted is not applied:
            # the box only shows it. Typing anything else, or clicking the button, is.
            if (
                name == "code"
                and value.strip() == form.get(f"suggested_{i}")
                and str(i) not in accepted
            ):
                continue
            setattr(row, name, value.strip())
        for name in ("subsystem", "task_type"):
            value = form.get(f"{name}_{i}")
            if isinstance(value, str):
                setattr(row, name, value or None)
        rows.append(row)
        included.append(f"include_{i}" in form)
    return rows, included


def _load_originals(form: FormData) -> list[importer.ParsedRow]:
    raw = json.loads(str(form.get("rows_json") or ""))
    if not isinstance(raw, list):
        raise TypeError("rows is not a list")
    return [_row_from_dict(item) for item in raw]


def _review_page(
    request: Request,
    user,
    originals: list[importer.ParsedRow],
    rows: list[importer.ParsedRow],
    included: list[bool],
    existing: set[str],
    *,
    filename: str,
    is_tracker: bool,
    banner: str | None = None,
    status: int = 200,
) -> Response:
    checks = importer.check_rows(rows, existing, included, [o.code for o in originals])
    reviews = [
        Review(i, row, originals[i], check, included[i])
        for i, (row, check) in enumerate(zip(rows, checks, strict=True))
    ]

    def count(state: str) -> int:
        return sum(1 for r in reviews if r.check.state == state)

    context = {
        "user": user,
        "filename": filename,
        "is_tracker": is_tracker,
        "reviews": reviews,
        "ready_count": count("add"),
        "fix_count": count("fix"),
        "exists_count": count("exists"),
        "left_out_count": count("left_out"),
        "subsystems": SUBSYSTEMS,
        "task_types": TASK_TYPES,
        "rows_json": json.dumps([asdict(o) for o in originals]),
        "banner": banner,
    }
    return templates.TemplateResponse(
        request, "admin_import_preview.html", context, status_code=status
    )


def _existing_codes(session: Session) -> set[str]:
    return set(session.exec(select(Task.code)).all())


@router.get("/import", response_class=HTMLResponse)
def import_form(request: Request, user: LeadUser):
    return _upload_page(request, user)


@router.post("/import/preview", response_class=HTMLResponse)
async def import_preview(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: LeadUser,
) -> Response:
    file = (await request.form()).get("file")
    if not isinstance(file, UploadFile) or not file.filename:
        return _upload_page(request, user, error="Choose a CSV file first.", status=400)

    try:
        data = await file.read(importer.MAX_BYTES + 1)
        parsed = importer.parse_tracker_csv(importer.decode_csv_bytes(data))
    except importer.ImportFileError as exc:
        return _upload_page(request, user, error=str(exc), status=400)

    try:
        existing = _existing_codes(session)
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Import preview could not read tasks (%s)", type(exc).__name__)
        return _upload_page(request, user, error=DB_ERROR, status=503)

    originals = parsed.rows
    return _review_page(
        request,
        user,
        originals,
        [_row_from_dict(asdict(o)) for o in originals],
        [True] * len(originals),
        existing,
        filename=file.filename,
        is_tracker=parsed.format == importer.FORMAT_TRACKER,
    )


@router.post("/import/check", response_class=HTMLResponse)
async def import_check(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: LeadUser,
) -> Response:
    """Re-check every row after the lead edits something. Saves nothing."""
    form = await request.form()
    try:
        originals = _load_originals(form)
    except (ValueError, TypeError):  # ValueError includes json.JSONDecodeError
        return _upload_page(request, user, error=STALE_PREVIEW, status=400)
    try:
        existing = _existing_codes(session)
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Import check could not read tasks (%s)", type(exc).__name__)
        return _upload_page(request, user, error=DB_ERROR, status=503)

    rows, included = _apply_edits(originals, form)
    return _review_page(
        request,
        user,
        originals,
        rows,
        included,
        existing,
        filename=str(form.get("filename") or "your file"),
        is_tracker=form.get("is_tracker") == "1",
    )


@router.post("/import/confirm", response_class=HTMLResponse)
async def import_confirm(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: LeadUser,
) -> Response:
    form = await request.form()
    try:
        originals = _load_originals(form)
    except (ValueError, TypeError):  # ValueError includes json.JSONDecodeError
        return _upload_page(request, user, error=STALE_PREVIEW, status=400)

    rows, included = _apply_edits(originals, form)
    try:
        existing = _existing_codes(session)
        checks = importer.check_rows(
            rows, existing, included, [o.code for o in originals]
        )
        if any(c.state == "fix" for c in checks):
            return _review_page(
                request,
                user,
                originals,
                rows,
                included,
                existing,
                filename=str(form.get("filename") or "your file"),
                is_tracker=form.get("is_tracker") == "1",
                banner="Nothing was saved. Fix the rows marked below, or leave them out.",
                status=400,
            )
        keep = [i for i, inc in enumerate(included) if inc]
        result = importer.save_rows(
            session, [rows[i] for i in keep], [originals[i].code for i in keep]
        )
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        session.rollback()
        log.error("Task import could not save (%s)", type(exc).__name__)
        return _upload_page(request, user, error=DB_ERROR, status=503)

    return templates.TemplateResponse(
        request, "admin_import_result.html", {"user": user, "result": result}
    )
