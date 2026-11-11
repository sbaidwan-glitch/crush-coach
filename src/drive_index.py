"""A local, searchable copy of the team's shared Drive.

The coach used to call the Drive API while a member waited. This module reads the
whole shared drive ahead of time (a full sync, then incremental syncs from the
Drive changes feed) and stores file metadata plus text chunks in the database,
so `search()` is one SQL query.

Everything here is read-only against Google: Drive, Docs and Sheets read-only
scopes only. One bad file never stops a sync: its text_status becomes "error"
with the reason, it is logged, and the sync moves on.
"""

import io
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import and_, delete, func, insert, or_, select, text, update
from sqlmodel import Session

import drive
from drive import DriveError
from models import DriveChunk, DriveFile, SyncState, utc_now

log = logging.getLogger("crush_coach")


class _QuietPdfRepairs(logging.Filter):
    """Drop pypdf's note about each slightly broken PDF it repairs by itself.

    Other pypdf warnings stay: "fontTools is required..." means some text in that
    PDF may be garbled, which is worth knowing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "wrong pointing object" not in record.getMessage()


logging.getLogger("pypdf").addFilter(_QuietPdfRepairs())

# --- limits -----------------------------------------------------------------

MAX_PDF_BYTES = 20 * 1024 * 1024  # bigger PDFs are skipped, with the reason recorded
MAX_TEXT_CHARS = 1_500_000  # per file; anything past this is dropped ("cut short")
MAX_SHEET_ROWS = 5000  # per tab
SHEET_RANGES_PER_CALL = 10
CHUNK_MIN_WORDS = 300
CHUNK_MAX_WORDS = 600
API_RETRIES = 4  # google-api-python-client backs off on 429 and 5xx by itself
DOCS_SHEETS_INTERVAL = 1.0  # seconds between Docs/Sheets reads (quota ~60/min/user)
STALE_LOCK_AFTER = timedelta(hours=2)  # a crashed sync can't block the next forever
INTERRUPTED = "The last sync was interrupted before it finished."
MAX_CONSECUTIVE_FAILURES = 10  # database writes failing back to back: stop
MAX_REPORTED_ERRORS = 25

GOOGLE_READONLY_SCOPES = [
    drive.DRIVE_READONLY_SCOPE,
    drive.DOCS_READONLY_SCOPE,
    drive.SHEETS_READONLY_SCOPE,
]
GOOGLE_SLIDES_MIME_TYPE = "application/vnd.google-apps.presentation"

# The live Drive search knows five CAD extensions; the index knows the usual rest.
CAD_EXTENSIONS = (
    *drive.CAD_EXTENSIONS,
    ".STL",
    ".IGES",
    ".IGS",
    ".X_T",
    ".X_B",
    ".3MF",
    ".DXF",
    ".DWG",
    ".F3D",
)

KINDS = ("doc", "sheet", "pdf", "cad", "folder", "other")
# Words a person uses for a kind of file, so "enclosure CAD" matches a .SLDPRT.
KIND_WORDS = {
    "cad": "cad model",
    "doc": "doc document",
    "sheet": "sheet spreadsheet",
    "pdf": "pdf",
    "folder": "folder",
}


class SyncError(RuntimeError):
    """A sync failed as a whole. The message is safe to show on a page."""


class SyncAlreadyRunning(SyncError):
    """Another sync holds the lock."""


@dataclass
class SyncReport:
    """What a sync did, for the script's summary line."""

    mode: str  # "full" or "incremental"
    files_seen: int = 0
    files_read: int = 0  # files whose text was (re)read this run
    removed: int = 0
    text_errors: int = 0
    errors: list[str] = field(default_factory=list)  # "name: reason", capped

    def add_error(self, name: str, reason: str) -> None:
        self.text_errors += 1
        if len(self.errors) < MAX_REPORTED_ERRORS:
            self.errors.append(f"{name}: {reason}")


# --- small helpers ----------------------------------------------------------


def parse_time(value: str | None) -> datetime | None:
    """A Google RFC 3339 time as naive UTC (how every timestamp is stored)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def classify(name: str, mime_type: str) -> str:
    """doc, sheet, pdf, cad, folder or other."""
    if mime_type == drive.GOOGLE_FOLDER_MIME_TYPE:
        return "folder"
    if name.upper().endswith(CAD_EXTENSIONS):
        return "cad"
    return drive._file_kind(name, mime_type)


def describe_error(exc: BaseException) -> str:
    """A short reason for a failure, safe to store and show. No tracebacks."""
    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", "?")
        reason = (getattr(exc, "reason", "") or "").strip()
        return f"Google API error {status}: {reason}"[:240]
    if isinstance(exc, DriveError | SyncError):
        return str(exc)[:240]
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {message}"[:240]


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: str) -> str:
    """Drop what Postgres can't store (NUL) or can't encode (lone surrogates)."""
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n")
    value = _CONTROL_CHARS.sub("", value)
    return value.encode("utf-8", "ignore").decode("utf-8")


def _word_count(value: str) -> int:
    return len(value.split())


# --- chunking ---------------------------------------------------------------


@dataclass
class Section:
    """Text under one heading (a doc heading, a sheet tab, a PDF page)."""

    heading: str | None
    text: str


@dataclass
class Chunk:
    heading: str | None
    text: str


def _split_long(value: str) -> list[str]:
    """Split text over the word limit into near-equal pieces, on line breaks."""
    total = _word_count(value)
    if total <= CHUNK_MAX_WORDS:
        return [value]
    pieces_needed = -(-total // CHUNK_MAX_WORDS)
    target = -(-total // pieces_needed)

    units: list[str] = []
    for line in value.split("\n"):
        words = line.split()
        if len(words) > target:  # one huge line (a PDF page with no breaks)
            units.extend(
                " ".join(words[i : i + target]) for i in range(0, len(words), target)
            )
        elif words:
            units.append(line)

    pieces: list[str] = []
    current: list[str] = []
    count = 0
    for unit in units:
        n = _word_count(unit)
        if current and count + n > target:
            pieces.append("\n".join(current))
            current, count = [], 0
        current.append(unit)
        count += n
    if current:
        pieces.append("\n".join(current))
    return pieces


def chunk_sections(sections: list[Section]) -> list[Chunk]:
    """Chunks of about 300-600 words, starting a new chunk at a heading.

    A section already over the minimum is never merged with the next one, so
    chunks follow the document's headings. Short sections are merged forward
    (with their heading kept as a line in the text) until a chunk is big enough,
    and a section over 600 words is split on line breaks.
    """
    chunks: list[Chunk] = []
    heading: str | None = None
    parts: list[str] = []
    words = 0

    def flush() -> None:
        nonlocal heading, parts, words
        if parts:
            chunks.append(Chunk(heading, "\n\n".join(parts)))
        heading, parts, words = None, [], 0

    for section in sections:
        body = section.text.strip()
        if not body:
            continue
        for i, piece in enumerate(_split_long(body)):
            n = _word_count(piece)
            starts_section = i == 0
            if parts and (
                words + n > CHUNK_MAX_WORDS
                or (starts_section and words >= CHUNK_MIN_WORDS)
            ):
                flush()
            if not parts:
                heading = section.heading
                parts.append(piece)
            elif starts_section and section.heading and section.heading != heading:
                parts.append(f"{section.heading}\n{piece}")
            else:
                parts.append(piece)
            words += n
    flush()
    return chunks


# --- text extraction --------------------------------------------------------


@dataclass
class Extracted:
    sections: list[Section] = field(default_factory=list)
    status: str = "ok"  # ok, skipped, error
    reason: str | None = None


_HEADING_STYLES = {"TITLE", "HEADING_1", "HEADING_2", "HEADING_3", "HEADING_4"}
_HEADING_STYLES |= {"HEADING_5", "HEADING_6"}


def _doc_blocks(content: list[dict[str, Any]]) -> list[tuple[str | None, str]]:
    """(paragraph style, text) for each paragraph and table row, in order."""
    blocks: list[tuple[str | None, str]] = []
    for element in content or []:
        paragraph = element.get("paragraph")
        if paragraph:
            runs = [
                part.get("textRun", {}).get("content", "")
                for part in paragraph.get("elements", [])
            ]
            style = paragraph.get("paragraphStyle", {}).get("namedStyleType")
            blocks.append((style, clean_text("".join(runs)).strip()))
        table = element.get("table")
        if table:
            for row in table.get("tableRows", []):
                cells = []
                for cell in row.get("tableCells", []):
                    inner = " ".join(t for _, t in _doc_blocks(cell.get("content", [])))
                    if inner.strip():
                        cells.append(inner.strip())
                if cells:
                    blocks.append((None, " | ".join(cells)))
        # tableOfContents, sectionBreak and the like carry no text worth indexing
    return blocks


def _walk_tabs(tabs: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """(tab title, body content) for every tab, child tabs included."""
    found: list[tuple[str, list[dict[str, Any]]]] = []
    for tab in tabs:
        title = tab.get("tabProperties", {}).get("title") or ""
        body = tab.get("documentTab", {}).get("body", {}).get("content", [])
        found.append((title, body))
        found.extend(_walk_tabs(tab.get("childTabs", [])))
    return found


def doc_sections(document: dict[str, Any]) -> list[Section]:
    """Sections of a Docs API document (read with includeTabsContent=True)."""
    if document.get("tabs"):
        tabs = _walk_tabs(document["tabs"])
    else:  # an older-style response: one body, no tabs
        tabs = [("", document.get("body", {}).get("content", []))]
    many_tabs = len(tabs) > 1

    sections: list[Section] = []
    for tab_title, content in tabs:
        prefix = tab_title if many_tabs and tab_title else None
        heading = prefix
        lines: list[str] = []

        def close(heading_now: str | None, lines_now: list[str]) -> None:
            if any(lines_now):
                sections.append(Section(heading_now, "\n".join(lines_now)))

        for style, block in _doc_blocks(content):
            if style in _HEADING_STYLES and block:
                close(heading, lines)
                lines = []
                heading = f"{prefix} › {block}" if prefix else block
            elif block:
                lines.append(block)
        close(heading, lines)
    return sections


def _column_letter(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def sheet_lines(rows: list[list[Any]]) -> list[str]:
    """One 'header: value; header: value' line per data row.

    The first non-empty row is the header. Blank cells are left out; a column
    without a header is labeled by its letter.
    """
    cleaned = [[clean_text(str(cell)).strip() for cell in row] for row in rows]
    start = next((i for i, row in enumerate(cleaned) if any(row)), None)
    if start is None:
        return []
    header = cleaned[start]
    lines: list[str] = []
    for row in cleaned[start + 1 : start + 1 + MAX_SHEET_ROWS]:
        pairs = [
            f"{header[j] if j < len(header) and header[j] else _column_letter(j)}: {cell}"
            for j, cell in enumerate(row)
            if cell
        ]
        if pairs:
            lines.append("; ".join(pairs))
    if not lines:  # a one-row sheet: the header is all there is
        lines = ["; ".join(cell for cell in header if cell)]
    return lines


def _quote_sheet_title(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


# --- Google access ----------------------------------------------------------


class GoogleServices:
    """The three Google APIs the index reads, plus a pace for Docs and Sheets."""

    def __init__(self, drive_api, docs_api, sheets_api, min_interval: float = 0.0):
        self.drive = drive_api
        self.docs = docs_api
        self.sheets = sheets_api
        self._min_interval = min_interval
        self._last_call = 0.0

    @classmethod
    def from_environment(cls) -> "GoogleServices":
        """Build read-only clients from the service-account environment variables."""
        credentials = drive._load_credentials(GOOGLE_READONLY_SCOPES)
        try:
            return cls(
                build("drive", "v3", credentials=credentials, cache_discovery=False),
                build("docs", "v1", credentials=credentials, cache_discovery=False),
                build("sheets", "v4", credentials=credentials, cache_discovery=False),
                min_interval=DOCS_SHEETS_INTERVAL,
            )
        except Exception:  # noqa: BLE001 (initialization failures need one safe error)
            raise DriveError("Google APIs could not be initialized.") from None

    def pace(self) -> None:
        """Wait, if needed, so Docs/Sheets reads stay under Google's quota."""
        wait = self._last_call + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()


def _run(request):
    return request.execute(num_retries=API_RETRIES)


def _extract_doc(services: GoogleServices, drive_id: str) -> Extracted:
    services.pace()
    document = _run(
        services.docs.documents().get(documentId=drive_id, includeTabsContent=True)
    )
    return _finish(doc_sections(document))


def _extract_sheet(services: GoogleServices, drive_id: str) -> Extracted:
    services.pace()
    meta = _run(
        services.sheets.spreadsheets().get(
            spreadsheetId=drive_id, fields="sheets.properties(title,sheetType)"
        )
    )
    titles = [
        sheet["properties"]["title"]
        for sheet in meta.get("sheets", [])
        if sheet.get("properties", {}).get("sheetType", "GRID") == "GRID"
    ]
    sections: list[Section] = []
    for i in range(0, len(titles), SHEET_RANGES_PER_CALL):
        batch = titles[i : i + SHEET_RANGES_PER_CALL]
        services.pace()
        response = _run(
            services.sheets.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=drive_id,
                ranges=[_quote_sheet_title(title) for title in batch],
            )
        )
        for title, value_range in zip(
            batch, response.get("valueRanges", []), strict=False
        ):
            lines = sheet_lines(value_range.get("values", []))
            if lines:
                sections.append(Section(title, "\n".join(lines)))
    return _finish(sections)


def _extract_pdf(services: GoogleServices, drive_id: str) -> Extracted:
    from pypdf import PdfReader  # imported here: only a sync reads PDFs

    data = _run(
        services.drive.files().get_media(fileId=drive_id, supportsAllDrives=True)
    )
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        return Extracted(status="skipped", reason="PDF is password-protected")

    sections: list[Section] = []
    unreadable_pages = 0
    for number, page in enumerate(reader.pages, start=1):
        try:
            page_text = clean_text(page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001 (one broken page must not lose the others)
            unreadable_pages += 1
            continue
        if page_text:
            sections.append(Section(f"Page {number}", page_text))
    if not sections:
        return Extracted(
            status="skipped",
            reason="PDF has no extractable text (a scan or drawing?)",
        )
    result = _finish(sections)
    if unreadable_pages:
        note = f"{unreadable_pages} page(s) could not be read"
        result.reason = f"{result.reason}; {note}" if result.reason else note
    return result


def _finish(sections: list[Section]) -> Extracted:
    """Apply the per-file text cap and decide ok vs skipped."""
    kept: list[Section] = []
    used = 0
    cut = False
    for section in sections:
        remaining = MAX_TEXT_CHARS - used
        if remaining <= 0:
            cut = True
            break
        body = section.text
        if len(body) > remaining:
            body, cut = body[:remaining], True
        kept.append(Section(section.heading, body))
        used += len(body)
    if not kept:
        return Extracted(status="skipped", reason="no text in this file")
    reason = f"text cut short at {MAX_TEXT_CHARS:,} characters" if cut else None
    return Extracted(kept, "ok", reason)


def extract(services: GoogleServices, info: dict[str, Any]) -> Extracted:
    """Read one file's text. Never raises: a failure comes back as status 'error'."""
    kind, mime_type = info["kind"], info["mime_type"]
    try:
        if kind == "folder":
            return Extracted(status="skipped", reason="folder")
        if kind == "cad":
            return Extracted(
                status="skipped",
                reason="CAD file: no text; name, folder and date are indexed",
            )
        if mime_type == drive.GOOGLE_DOC_MIME_TYPE:
            return _extract_doc(services, info["drive_id"])
        if mime_type == drive.GOOGLE_SHEET_MIME_TYPE:
            return _extract_sheet(services, info["drive_id"])
        if kind == "pdf":
            size = info.get("size")
            if size is not None and size > MAX_PDF_BYTES:
                megabytes = size / (1024 * 1024)
                return Extracted(
                    status="skipped",
                    reason=(
                        f"PDF is {megabytes:.0f} MB, over the "
                        f"{MAX_PDF_BYTES // (1024 * 1024)} MB limit"
                    ),
                )
            return _extract_pdf(services, info["drive_id"])
        if kind in ("doc", "sheet"):
            return Extracted(
                status="skipped",
                reason="Office file: only Google Docs and Sheets are read",
            )
        return Extracted(status="skipped", reason=f"text not read for {mime_type}")
    except Exception as exc:  # noqa: BLE001 (one bad file must not stop the sync)
        reason = describe_error(exc)
        log.warning(
            "Drive index: could not read %s %s (%s)",
            kind,
            info["drive_id"],
            reason,
        )
        return Extracted(status="error", reason=reason)


# --- folder paths -----------------------------------------------------------


def folder_path_for(
    parent_id: str | None, folders: dict[str, tuple[str, str | None]]
) -> str:
    """'Drive / Mechanical / CAD' for a parent folder id; '/' when unknown.

    `folders` maps a folder id to (name, parent id). Matches the style of the
    live Drive search so paths read the same everywhere.
    """
    names: list[str] = []
    seen: set[str] = set()
    current = parent_id
    while current and current not in seen and current in folders:
        seen.add(current)
        name, current = folders[current]
        names.append(name)
    return " / ".join(reversed(names)) or "/"


def _file_info(item: dict[str, Any]) -> dict[str, Any]:
    """The columns a Drive file resource fills in (everything but folder_path)."""
    drive_id = item["id"]
    name = item.get("name") or "Untitled"
    mime_type = item.get("mimeType") or "application/octet-stream"
    parents = item.get("parents") or []
    size = item.get("size")
    return {
        "drive_id": drive_id,
        "name": name,
        "mime_type": mime_type,
        "parent_id": parents[0] if parents else None,
        "modified_time": parse_time(item.get("modifiedTime")),
        "web_link": item.get("webViewLink")
        or f"https://drive.google.com/open?id={drive_id}",
        "size": int(size) if str(size or "").isdigit() else None,
        "kind": classify(name, mime_type),
    }


# --- sync state and the lock -----------------------------------------------


def get_state(session: Session) -> SyncState:
    """The single sync_state row, created on first use."""
    state = session.get(SyncState, 1)
    if state is None:
        state = SyncState(id=1)
        session.add(state)
        session.commit()
    return state


def claim(session: Session) -> None:
    """Take the sync lock, or raise SyncAlreadyRunning. Atomic on both databases."""
    get_state(session)
    now = utc_now()
    claimed = session.execute(
        update(SyncState)
        .where(
            SyncState.id == 1,
            or_(
                SyncState.running_since.is_(None),
                SyncState.running_since < now - STALE_LOCK_AFTER,
            ),
        )
        .values(running_since=now)
    )
    session.commit()
    if claimed.rowcount != 1:
        raise SyncAlreadyRunning("A Drive sync is already running.")


def release(session: Session, error: str | None) -> None:
    session.rollback()
    session.execute(
        update(SyncState)
        .where(SyncState.id == 1)
        .values(running_since=None, last_error=error)
    )
    session.commit()


def release_quietly(session: Session, error: str | None) -> None:
    """Release the lock without ever raising, so it can't hide the real error.

    If even this fails (the database is down), the lock simply goes stale and the
    next sync takes it over after STALE_LOCK_AFTER.
    """
    try:
        release(session, error)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Could not release the Drive sync lock (%s); it expires in %s",
            type(exc).__name__,
            STALE_LOCK_AFTER,
        )


def refresh_counts(session: Session) -> None:
    """Store the current file, chunk and text-error counts on the state row."""
    files = session.scalar(select(func.count()).select_from(DriveFile))
    chunks = session.scalar(select(func.count()).select_from(DriveChunk))
    errors = session.scalar(
        select(func.count())
        .select_from(DriveFile)
        .where(DriveFile.text_status == "error")
    )
    session.execute(
        update(SyncState)
        .where(SyncState.id == 1)
        .values(file_count=files, chunk_count=chunks, error_count=errors)
    )


# --- writing the index ------------------------------------------------------


def _delete_files(session: Session, file_ids: list[int]) -> None:
    for i in range(0, len(file_ids), 500):
        batch = file_ids[i : i + 500]
        session.execute(delete(DriveChunk).where(DriveChunk.file_id.in_(batch)))
        session.execute(delete(DriveFile).where(DriveFile.id.in_(batch)))
    session.commit()


def _replace_chunks(session: Session, file_id: int, chunks: list[Chunk]) -> None:
    session.execute(delete(DriveChunk).where(DriveChunk.file_id == file_id))
    if chunks:
        # Core insert on the table, so the generated full-text column is left alone.
        session.execute(
            insert(DriveChunk.__table__),
            [
                {
                    "file_id": file_id,
                    "position": position,
                    "heading": chunk.heading,
                    "text": chunk.text,
                }
                for position, chunk in enumerate(chunks)
            ],
        )


@dataclass(frozen=True)
class _Known:
    """What the index remembers about a file, as plain values (rows expire on commit)."""

    id: int
    modified_time: datetime | None
    size: int | None
    mime_type: str
    text_status: str
    folder_path: str
    name: str
    parent_id: str | None


def _known_files(session: Session) -> dict[str, _Known]:
    rows = session.execute(
        select(
            DriveFile.drive_id,
            DriveFile.id,
            DriveFile.modified_time,
            DriveFile.size,
            DriveFile.mime_type,
            DriveFile.text_status,
            DriveFile.folder_path,
            DriveFile.name,
            DriveFile.parent_id,
        )
    ).all()
    return {row[0]: _Known(*row[1:]) for row in rows}


def _folder_map(session: Session) -> dict[str, tuple[str, str | None]]:
    rows = session.execute(
        select(DriveFile.drive_id, DriveFile.name, DriveFile.parent_id).where(
            DriveFile.kind == "folder"
        )
    ).all()
    return {drive_id: (name, parent) for drive_id, name, parent in rows}


def _upsert_roots(session: Session, services: GoogleServices) -> set[str]:
    """Store each shared drive's root as a folder, so paths start with its name."""
    roots: dict[str, str] = {}
    page_token = None
    while True:
        parameters: dict[str, Any] = {
            "pageSize": 100,
            "fields": "nextPageToken,drives(id,name)",
        }
        if page_token:
            parameters["pageToken"] = page_token
        response = _run(services.drive.drives().list(**parameters))
        for shared in response.get("drives", []):
            roots[shared["id"]] = shared.get("name") or "Shared drive"
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    known = _known_files(session)
    for root_id, name in roots.items():
        row = session.get(DriveFile, known[root_id].id) if root_id in known else None
        if row is None:
            row = DriveFile(
                drive_id=root_id,
                name=name,
                mime_type=drive.GOOGLE_FOLDER_MIME_TYPE,
                folder_path="/",
                web_link=f"https://drive.google.com/drive/folders/{root_id}",
                kind="folder",
                text_status="skipped",
                text_reason="folder",
            )
        row.name = name
        row.last_indexed_at = utc_now()
        session.add(row)
    session.commit()
    return set(roots)


def _upsert_folders(session: Session, items: list[dict[str, Any]]) -> bool:
    """Store the folders among `items`. True when any folder is new or moved."""
    known = _known_files(session)
    changed = False
    for item in items:
        info = _file_info(item)
        if info["kind"] != "folder":
            continue
        before = known.get(info["drive_id"])
        if before and (before.name, before.parent_id) == (
            info["name"],
            info["parent_id"],
        ):
            continue
        changed = True
        row = (
            session.get(DriveFile, before.id)
            if before
            else DriveFile(
                **info, folder_path="/", text_status="skipped", text_reason="folder"
            )
        )
        for key, value in info.items():
            setattr(row, key, value)
        row.last_indexed_at = utc_now()
        session.add(row)
    session.commit()
    return changed


def _refresh_paths(session: Session, only: set[str] | None = None) -> None:
    """Recompute folder_path from the stored folders (all rows, or just `only`)."""
    folders = _folder_map(session)
    query = select(
        DriveFile.id, DriveFile.drive_id, DriveFile.parent_id, DriveFile.folder_path
    )
    updates = []
    for file_id, drive_id, parent_id, current in session.execute(query).all():
        if only is not None and drive_id not in only:
            continue
        path = folder_path_for(parent_id, folders)
        if path != current:
            updates.append({"file_id": file_id, "path": path})
    for update_row in updates:
        session.execute(
            update(DriveFile)
            .where(DriveFile.id == update_row["file_id"])
            .values(folder_path=update_row["path"])
        )
    session.commit()


def _index_file(
    session: Session,
    services: GoogleServices,
    item: dict[str, Any],
    known: _Known | None,
    folders: dict[str, tuple[str, str | None]],
    force: bool,
    report: SyncReport,
) -> None:
    """Store one file's metadata and, if it is new or changed, its text chunks."""
    info = _file_info(item)
    info["folder_path"] = folder_path_for(info["parent_id"], folders)
    changed = (
        known is None
        or force
        or known.modified_time != info["modified_time"]
        or known.size != info["size"]
        or known.mime_type != info["mime_type"]
        or known.text_status == "error"
    )

    extracted: Extracted | None = None
    if changed:
        extracted = extract(services, info)
        report.files_read += 1
        if extracted.status == "error":
            report.add_error(info["name"], extracted.reason or "unknown error")

    row = (
        session.get(DriveFile, known.id)
        if known
        else DriveFile(**info, text_status="skipped")
    )
    for key, value in info.items():
        setattr(row, key, value)
    if extracted is not None:
        row.text_status = extracted.status
        row.text_reason = extracted.reason
        row.last_indexed_at = utc_now()
    session.add(row)
    session.flush()
    if extracted is not None:
        _replace_chunks(session, row.id, chunk_sections(extracted.sections))
    session.commit()


def _index_files(
    session: Session,
    services: GoogleServices,
    items: list[dict[str, Any]],
    force: bool,
    report: SyncReport,
) -> None:
    """Index every non-folder item. A failure on one file is logged; the rest go on."""
    known = _known_files(session)
    folders = _folder_map(session)
    failures_in_a_row = 0
    for item in items:
        if not item.get("id") or _file_info(item)["kind"] == "folder":
            continue
        try:
            _index_file(
                session, services, item, known.get(item["id"]), folders, force, report
            )
            failures_in_a_row = 0
        except Exception as exc:  # one bad file must not stop the sync
            session.rollback()
            failures_in_a_row += 1
            reason = describe_error(exc)
            log.warning("Drive index: could not save %s (%s)", item.get("id"), reason)
            report.add_error(item.get("name") or item["id"], f"not saved: {reason}")
            if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                raise SyncError(
                    "The database rejected several files in a row, so the sync stopped."
                ) from exc


# --- sync entry points ------------------------------------------------------

LIST_FIELDS = "id,name,mimeType,modifiedTime,webViewLink,size,parents"


def _list_everything(services: GoogleServices) -> list[dict[str, Any]]:
    """Every non-trashed file and folder in every drive the account can see."""
    items: list[dict[str, Any]] = []
    page_token = None
    while True:
        parameters: dict[str, Any] = {
            "q": "trashed = false",
            "pageSize": 1000,
            "fields": f"nextPageToken,files({LIST_FIELDS})",
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
            "corpora": "allDrives",
        }
        if page_token:
            parameters["pageToken"] = page_token
        response = _run(services.drive.files().list(**parameters))
        items.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return items


def _start_page_token(services: GoogleServices) -> str:
    response = _run(services.drive.changes().getStartPageToken(supportsAllDrives=True))
    token = response.get("startPageToken")
    if not token:
        raise SyncError("Google Drive gave no changes page token.")
    return token


def _run_full(session: Session, services: GoogleServices, force: bool) -> SyncReport:
    report = SyncReport("full")
    # Take the token first: anything that changes during the walk is then replayed
    # by the next incremental sync (re-indexing is harmless), never missed.
    token = _start_page_token(services)
    root_ids = _upsert_roots(session, services)
    items = _list_everything(services)
    indexed = session.scalar(select(func.count()).select_from(DriveFile)) or 0
    if not items and indexed > len(root_ids):
        # An empty listing over a non-empty index means lost access, not a deleted
        # drive. Refuse to wipe the index on that evidence.
        raise SyncError("Google Drive listed no files; the existing index was kept.")

    report.files_seen = len(items)
    _upsert_folders(session, items)
    _refresh_paths(session)
    _index_files(session, services, items, force, report)

    seen = {item["id"] for item in items if item.get("id")} | root_ids
    stale = [
        known.id
        for drive_id, known in _known_files(session).items()
        if drive_id not in seen
    ]
    _delete_files(session, stale)
    report.removed = len(stale)

    session.execute(
        update(SyncState)
        .where(SyncState.id == 1)
        .values(page_token=token, last_full_sync=utc_now())
    )
    return report


def _list_changes(
    services: GoogleServices, token: str
) -> tuple[list[dict[str, Any]], str]:
    changes: list[dict[str, Any]] = []
    page_token: str | None = token
    while True:
        response = _run(
            services.drive.changes().list(
                pageToken=page_token,
                pageSize=500,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                includeRemoved=True,
                fields=(
                    "nextPageToken,newStartPageToken,"
                    "changes(changeType,removed,fileId,drive(id,name),"
                    f"file({LIST_FIELDS},trashed))"
                ),
            )
        )
        changes.extend(response.get("changes", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            new_token = response.get("newStartPageToken")
            if not new_token:
                raise SyncError("Google Drive gave no new changes page token.")
            return changes, new_token


def _descendant_ids(session: Session, folder_drive_ids: set[str]) -> set[str]:
    """Drive ids of everything inside the given folders, at any depth."""
    rows = session.execute(select(DriveFile.drive_id, DriveFile.parent_id)).all()
    children: dict[str, list[str]] = {}
    for drive_id, parent_id in rows:
        if parent_id:
            children.setdefault(parent_id, []).append(drive_id)
    found: set[str] = set()
    stack = list(folder_drive_ids)
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _run_incremental(
    session: Session, services: GoogleServices, state: SyncState
) -> SyncReport:
    report = SyncReport("incremental")
    try:
        changes, new_token = _list_changes(services, state.page_token)
    except HttpError as exc:
        if getattr(exc.resp, "status", None) in (400, 404, 410):
            # The stored token is no longer valid; start over from a full sync.
            log.warning(
                "Drive index: stored changes token rejected; running a full sync"
            )
            return _run_full(session, services, force=False)
        raise

    try:
        _upsert_roots(session, services)
    except Exception as exc:  # noqa: BLE001 (stored root names are good enough)
        session.rollback()
        log.warning(
            "Drive index: could not refresh drive names (%s)", describe_error(exc)
        )

    removed_ids: set[str] = set()
    changed_items: dict[str, dict[str, Any]] = {}
    for change in changes:
        if change.get("changeType") == "drive":
            continue  # roots were refreshed above
        file_id = change.get("fileId") or change.get("file", {}).get("id")
        if not file_id:
            continue
        file = change.get("file") or {}
        if change.get("removed") or file.get("trashed") or not file:
            removed_ids.add(file_id)
            changed_items.pop(file_id, None)
        else:
            removed_ids.discard(file_id)
            changed_items[file_id] = file
    report.files_seen = len(changes)

    # Trashing a folder takes its contents with it.
    known = _known_files(session)
    gone_folders = {i for i in removed_ids if i in known}
    removed_ids |= _descendant_ids(session, gone_folders)
    ids_to_delete = [known[i].id for i in removed_ids if i in known]
    _delete_files(session, ids_to_delete)
    report.removed = len(ids_to_delete)

    items = list(changed_items.values())
    folders_changed = _upsert_folders(session, items)
    _refresh_paths(session)  # cheap, and keeps files right when a folder moved
    if folders_changed:
        log.info("Drive index: a folder changed; paths were recomputed")
    _index_files(session, services, items, False, report)

    session.execute(
        update(SyncState)
        .where(SyncState.id == 1)
        .values(page_token=new_token, last_incremental_sync=utc_now())
    )
    return report


def _locked_sync(
    session: Session,
    services: GoogleServices | None,
    mode: str,
    force: bool,
    already_claimed: bool,
) -> SyncReport:
    if not already_claimed:
        claim(session)  # raises SyncAlreadyRunning without touching the running sync
    # Whatever happens from here on, the lock is released in `finally`. If the
    # process is interrupted (Ctrl-C, SIGTERM) the sync never gets to set `error`,
    # so this is what gets recorded.
    error: str | None = INTERRUPTED
    try:
        services = services or GoogleServices.from_environment()
        state = get_state(session)
        if mode == "incremental" and state.page_token:
            report = _run_incremental(session, services, state)
        else:
            # No token yet (or an unusable one): the first sync has to be a full one.
            report = _run_full(session, services, force)
        refresh_counts(session)
        session.commit()
        error = None
        return report
    except Exception as exc:
        error = describe_error(exc)
        log.error("Drive %s sync failed (%s)", mode, error)
        if isinstance(exc, SyncError):
            raise
        raise SyncError(error) from exc
    finally:
        release_quietly(session, error)


def full_sync(
    session: Session,
    services: GoogleServices | None = None,
    *,
    force: bool = False,
    already_claimed: bool = False,
) -> SyncReport:
    """Walk the whole shared drive and (re)index it.

    Files whose modified time hasn't changed keep their stored text, unless
    `force` is set or their last read failed. Files no longer in the drive are
    removed. Raises SyncError if the sync as a whole fails.
    """
    return _locked_sync(session, services, "full", force, already_claimed)


def incremental_sync(
    session: Session,
    services: GoogleServices | None = None,
    *,
    already_claimed: bool = False,
) -> SyncReport:
    """Apply what changed in Drive since the last sync (full, if there was none).

    `already_claimed`: the caller already took the lock with `claim()` (the admin
    button does, so its page can show "running" straight away).
    """
    return _locked_sync(session, services, "incremental", False, already_claimed)


# --- search -----------------------------------------------------------------

NAME_ALL_BOOST = 0.5  # every query word is in the file's name/folder/kind
NAME_ANY_BOOST = 0.2  # some query word is
SNIPPET_CHARS = 240
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class SearchHit:
    """One result: a chunk of a file, or just the file when only its name matched."""

    file_id: int
    drive_id: str
    name: str
    kind: str
    folder_path: str
    web_link: str | None
    modified_time: datetime | None
    heading: str | None
    text: str  # the whole chunk; empty for a name-only hit
    snippet: str
    score: float
    chunk_id: int | None = None


def make_snippet(body: str, words: list[str], width: int = SNIPPET_CHARS) -> str:
    """A short excerpt of `body` around the first query word found in it."""
    flat = " ".join(body.split())
    if len(flat) <= width:
        return flat
    lowered = flat.casefold()
    positions = [p for p in (lowered.find(w.casefold()) for w in words) if p >= 0]
    start = max(min(positions, default=0) - width // 4, 0)
    excerpt = flat[start : start + width].strip()
    return ("…" if start else "") + excerpt + ("…" if start + width < len(flat) else "")


_FILE_TEXT_SQL = (
    "to_tsvector('english', "
    "regexp_replace(f.name || ' ' || f.folder_path, '[_./-]+', ' ', 'g') || ' ' || "
    "CASE f.kind "
    + " ".join(f"WHEN '{kind}' THEN '{words}'" for kind, words in KIND_WORDS.items())
    + " ELSE '' END)"
)

_PG_CHUNK_QUERY = text(
    f"""
    WITH q AS (
        SELECT websearch_to_tsquery('english', :q) AS tsq,
               websearch_to_tsquery('english', :q_all) AS tsq_all,
               websearch_to_tsquery('english', :q_any) AS tsq_any
    )
    SELECT c.id AS chunk_id, c.heading, c.text, f.id AS file_id, f.drive_id, f.name,
           f.kind, f.folder_path, f.web_link, f.modified_time,
           ts_rank(c.search_vector, q.tsq, 32)
             + CASE WHEN n.v @@ q.tsq_all THEN {NAME_ALL_BOOST}
                    WHEN n.v @@ q.tsq_any THEN {NAME_ANY_BOOST} ELSE 0 END AS score
    FROM q, drive_chunk c
    JOIN drive_file f ON f.id = c.file_id
    CROSS JOIN LATERAL (SELECT {_FILE_TEXT_SQL} AS v) n
    WHERE c.search_vector @@ q.tsq
    ORDER BY score DESC, f.modified_time DESC NULLS LAST, c.position
    LIMIT :n
    """
)

_PG_FILE_QUERY = text(
    f"""
    WITH q AS (
        SELECT websearch_to_tsquery('english', :q) AS tsq,
               websearch_to_tsquery('english', :q_all) AS tsq_all
    )
    SELECT f.id AS file_id, f.drive_id, f.name, f.kind, f.folder_path, f.web_link,
           f.modified_time,
           CASE WHEN n.v @@ q.tsq_all THEN {NAME_ALL_BOOST} ELSE {NAME_ANY_BOOST} END
             + 0.3 * ts_rank(n.v, q.tsq, 32) AS score
    FROM q, drive_file f
    CROSS JOIN LATERAL (SELECT {_FILE_TEXT_SQL} AS v) n
    WHERE n.v @@ q.tsq
    ORDER BY score DESC, f.modified_time DESC NULLS LAST
    LIMIT :n
    """
)


def _or_query(words: list[str]) -> str:
    return " or ".join(words)


def _postgres_hits(
    session: Session, query: str, words: list[str], fetch: int
) -> list[SearchHit]:
    def run(q: str) -> list[SearchHit]:
        any_q = _or_query(words)
        chunk_rows = (
            session.execute(
                _PG_CHUNK_QUERY,
                {"q": q, "q_all": query, "q_any": any_q, "n": fetch},
            )
            .mappings()
            .all()
        )
        hits = [
            SearchHit(
                file_id=r["file_id"],
                drive_id=r["drive_id"],
                name=r["name"],
                kind=r["kind"],
                folder_path=r["folder_path"],
                web_link=r["web_link"],
                modified_time=r["modified_time"],
                heading=r["heading"],
                text=r["text"],
                snippet=make_snippet(r["text"], words),
                score=float(r["score"]),
                chunk_id=r["chunk_id"],
            )
            for r in chunk_rows
        ]
        with_chunks = {hit.file_id for hit in hits}
        file_rows = (
            session.execute(_PG_FILE_QUERY, {"q": q, "q_all": query, "n": fetch})
            .mappings()
            .all()
        )
        hits.extend(
            SearchHit(
                file_id=r["file_id"],
                drive_id=r["drive_id"],
                name=r["name"],
                kind=r["kind"],
                folder_path=r["folder_path"],
                web_link=r["web_link"],
                modified_time=r["modified_time"],
                heading=None,
                text="",
                snippet="",
                score=float(r["score"]),
            )
            for r in file_rows
            if r["file_id"] not in with_chunks
        )
        return hits

    hits = run(query)
    if not hits and len(words) > 1:  # "where's the latest enclosure CAD": loosen to OR
        hits = run(_or_query(words))
    return hits


def _like_hits(session: Session, words: list[str], fetch: int) -> list[SearchHit]:
    """The SQLite fallback (tests, and a laptop without Postgres): plain LIKE."""

    def run(match_all: bool) -> list[SearchHit]:
        join = and_ if match_all else or_
        file_text = (
            func.coalesce(DriveFile.name, "")
            + " "
            + func.coalesce(DriveFile.folder_path, "")
            + " "
            + func.coalesce(DriveFile.kind, "")
        )
        kind_hits = [
            DriveFile.kind == kind
            for word in words
            for kind, kind_words in KIND_WORDS.items()
            if word.casefold() in kind_words.split()
        ]
        in_file = [
            or_(file_text.icontains(word, autoescape=True), *kind_hits)
            for word in words
        ]
        in_chunk = [
            or_(
                DriveChunk.text.icontains(word, autoescape=True),
                func.coalesce(DriveChunk.heading, "").icontains(word, autoescape=True),
            )
            for word in words
        ]

        def name_boost(file_name: str, path: str, kind: str) -> float:
            haystack = f"{file_name} {path} {kind}".casefold()
            matched = [w for w in words if w.casefold() in haystack]
            if len(matched) == len(words):
                return NAME_ALL_BOOST
            return NAME_ANY_BOOST if matched else 0.0

        chunk_rows = session.execute(
            select(DriveChunk, DriveFile)
            .join(DriveFile, DriveFile.id == DriveChunk.file_id)
            .where(join(*in_chunk))
            .limit(fetch * 4)
        ).all()
        hits = []
        for chunk, file in chunk_rows:
            body = f"{chunk.heading or ''} {chunk.text}".casefold()
            share = sum(w.casefold() in body for w in words) / len(words)
            hits.append(
                SearchHit(
                    file_id=file.id,
                    drive_id=file.drive_id,
                    name=file.name,
                    kind=file.kind,
                    folder_path=file.folder_path,
                    web_link=file.web_link,
                    modified_time=file.modified_time,
                    heading=chunk.heading,
                    text=chunk.text,
                    snippet=make_snippet(chunk.text, words),
                    score=0.4 * share
                    + name_boost(file.name, file.folder_path, file.kind),
                    chunk_id=chunk.id,
                )
            )
        with_chunks = {hit.file_id for hit in hits}
        file_rows = session.execute(
            select(DriveFile).where(join(*in_file)).limit(fetch * 4)
        ).scalars()
        hits.extend(
            SearchHit(
                file_id=file.id,
                drive_id=file.drive_id,
                name=file.name,
                kind=file.kind,
                folder_path=file.folder_path,
                web_link=file.web_link,
                modified_time=file.modified_time,
                heading=None,
                text="",
                snippet="",
                score=name_boost(file.name, file.folder_path, file.kind),
            )
            for file in file_rows
            if file.id not in with_chunks
        )
        return hits

    hits = run(True)
    if not hits and len(words) > 1:
        hits = run(False)
    return hits


def search(
    session: Session,
    query: str,
    limit: int = 10,
    *,
    max_per_file: int | None = 3,
) -> list[SearchHit]:
    """Ranked chunks (or name-only file hits) for a question's keywords.

    Postgres: websearch_to_tsquery + ts_rank, with a boost when the query words
    are in the file's name, folder path or kind. When nothing matches every word,
    it retries matching any word. Scores that agree to three decimals are broken
    by the newer file. SQLite falls back to LIKE. `max_per_file` stops one long
    document from filling the list.
    """
    words = [w for w in _WORD.findall(query) if any(c.isalnum() for c in w)]
    if limit <= 0 or not words:
        return []
    fetch = max(limit * 5, 50)
    if session.get_bind().dialect.name == "postgresql":
        hits = _postgres_hits(session, query, words, fetch)
    else:
        hits = _like_hits(session, words, fetch)

    oldest = datetime.fromtimestamp(0, UTC).replace(tzinfo=None)
    hits.sort(
        key=lambda hit: (round(hit.score, 3), hit.modified_time or oldest),
        reverse=True,
    )
    results: list[SearchHit] = []
    per_file: dict[int, int] = {}
    for hit in hits:
        if max_per_file is not None and per_file.get(hit.file_id, 0) >= max_per_file:
            continue
        per_file[hit.file_id] = per_file.get(hit.file_id, 0) + 1
        results.append(hit)
        if len(results) >= limit:
            break
    return results
