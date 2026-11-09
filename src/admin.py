"""Lead-only pages. `/admin/import` loads tasks from the team's tracker CSV.

Flow: upload -> preview (nothing saved) -> Import. The preview page carries the
parsed rows in a hidden field, so the server stores nothing between steps. The
confirm step re-validates every row; the browser is never trusted.
"""

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.datastructures import UploadFile

import db
import import_tasks as importer
from auth import LeadUser
from models import SUBSYSTEMS, TASK_TYPES, Task

log = logging.getLogger("crush_coach")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter(prefix="/admin")

DB_ERROR = "Couldn't reach the database right now. Nothing was saved. Try again in a moment."
STALE_PREVIEW = (
    "The preview data was missing or damaged, so nothing was saved. "
    "Upload the file again."
)


def _upload_page(request: Request, user, *, error: str | None = None, status=200):
    return templates.TemplateResponse(
        request, "admin_import.html", {"user": user, "error": error}, status_code=status
    )


def _row_from_dict(data: Any) -> importer.ParsedRow:
    """Rebuild a row from the preview's hidden field, coercing every type."""
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
        error=text(data.get("error")),
    )


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
        existing = set(session.exec(select(Task.code)).all())
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        log.error("Import preview could not read tasks (%s)", type(exc).__name__)
        return _upload_page(request, user, error=DB_ERROR, status=503)

    rows = [
        {"index": i, "row": r, "exists": not r.error and r.code in existing}
        for i, r in enumerate(parsed.rows)
    ]
    problems = [r for r in parsed.rows if r.error]
    context = {
        "user": user,
        "filename": file.filename,
        "is_tracker": parsed.format == importer.FORMAT_TRACKER,
        "rows": rows,
        "problems": problems,
        "ready_count": sum(
            1
            for r in rows
            if not r["row"].error and not r["exists"] and r["row"].subsystem
        ),
        "needs_subsystem_count": sum(
            1
            for r in rows
            if not r["row"].error and not r["exists"] and not r["row"].subsystem
        ),
        "exists_count": sum(1 for r in rows if r["exists"]),
        "subsystems": SUBSYSTEMS,
        "task_types": TASK_TYPES,
        "rows_json": json.dumps([asdict(r) for r in parsed.rows]),
    }
    return templates.TemplateResponse(request, "admin_import_preview.html", context)


@router.post("/import/confirm", response_class=HTMLResponse)
async def import_confirm(
    request: Request,
    session: Annotated[Session, Depends(db.get_session)],
    user: LeadUser,
) -> Response:
    form = await request.form()
    try:
        raw = json.loads(str(form.get("rows_json") or ""))
        if not isinstance(raw, list):
            raise TypeError("rows is not a list")
        rows = [_row_from_dict(item) for item in raw]
    except (ValueError, TypeError):  # ValueError includes json.JSONDecodeError
        return _upload_page(request, user, error=STALE_PREVIEW, status=400)

    # Apply the lead's corrections from the dropdowns.
    for i, row in enumerate(rows):
        if row.error:
            continue
        subsystem = form.get(f"subsystem_{i}")
        task_type = form.get(f"task_type_{i}")
        if isinstance(subsystem, str):
            row.subsystem = subsystem or None
        if isinstance(task_type, str):
            row.task_type = task_type or None

    try:
        result = importer.save_rows(session, rows)
    except Exception as exc:  # noqa: BLE001  (a DB failure must not break the page)
        session.rollback()
        log.error("Task import could not save (%s)", type(exc).__name__)
        return _upload_page(request, user, error=DB_ERROR, status=503)

    return templates.TemplateResponse(
        request, "admin_import_result.html", {"user": user, "result": result}
    )
