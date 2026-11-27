"""Writing to Google: the only module with a Google write scope.

The Crush document (`DECISIONS_DOC_ID`) is the team's running log. Two things are added
to it, each as one entry under a tab and a date heading:

- a member's **notebook entry**, as "Name · CODE: what. Why: ..." plus the specs they
  accepted and a link to each photo (published without approval);
- a **recorded answer**, when a lead records it: a bold statement, why, what it replaces,
  sources and who recorded it.

An entry's photos are uploaded first, to a "Notebook photos" folder inside the writable
Drive folder (`DRIVE_WRITE_FOLDER_ID`), with the narrow `drive.file` scope (the app can only
touch files it made). If an upload fails the text is still published and the photos stay in
the app.

Rules this module keeps:

- **Append-only.** It only ever sends `insertText` and styling requests for the text it
  inserts. Nothing existing is edited or deleted, and there is no code path that could.
- **Under the right tab and day.** The entry goes under the tab named for its subsystem
  (or for its part of Crush: "Actuators" for Gripper and Torpedo), below that day's date
  heading ("17 Nov 2026"; the heading is created when the day has none). If no tab
  matches, the "General" tab is used, and the result says so (`used_general`).
- **Once.** A `DriveWrite` row per entry makes a write idempotent: a repeated request, a
  double click or a retry never adds a second copy. If the text is already under that
  day's heading (a write that succeeded but wasn't recorded), it is marked written.
- **Failure is visible and recoverable.** A failed write keeps the entry in the app; the
  row says why, and `retry` tries again. A Google call never breaks the page.
- **Safe to race.** Edits carry the revision the position was computed from; if someone
  edited the document meanwhile, the position is recomputed.

The Drive index never reads this document back (`drive_exclude.py`), so an entry can't
come back as new evidence against the record it was written from.

Functions take plain values and a Session so tests can use them with a fake Docs client.
"""

import io
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import google_auth_httplib2
import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer
from sqlmodel import Session, col, select, update

import drive
import localtime
from drive_exclude import CRUSH_DOC_ENV_VAR
from models import (
    Decision,
    DecisionTask,
    DriveWrite,
    NotePhoto,
    NoteSpec,
    Task,
    WorkNote,
    utc_now,
)
from progress import SUBSYSTEM_GROUPS

log = logging.getLogger("crush_coach")

# The one write scope in the app. Every other module asks Google for read-only access.
DOCS_WRITE_SCOPE = "https://www.googleapis.com/auth/documents"
# Photos: only files this app created (not the whole Drive).
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
WRITE_FOLDER_ENV_VAR = "DRIVE_WRITE_FOLDER_ID"  # the Drive folder shared with the app as Editor
PHOTO_FOLDER_NAME = "Notebook photos"
FOLDER_MIME = "application/vnd.google-apps.folder"
GENERAL_TAB = "General"
DATE_HEADING_STYLE = "HEADING_3"
BODY_STYLE = "NORMAL_TEXT"
RESET_TEXT_STYLE = (
    "bold,italic,underline,strikethrough,link,foregroundColor,backgroundColor,"
    "fontSize,weightedFontFamily"
)
# A write can take minutes in the worst case (3 reads and 3 writes, each retried); a
# "writing" row older than this belongs to a request that died.
STALE_WRITE_AFTER = timedelta(minutes=15)
REVISION_TRIES = 3
REQUEST_TIMEOUT_SECONDS = 20  # one Google call; a hung connection must not hold the page
MAX_FIELD_CHARS = 1500

NOT_CONFIGURED = (
    "The Crush document isn't set up yet (DECISIONS_DOC_ID is missing), so nothing was "
    "written to it."
)

# Other words a tab may use for a subsystem (compared after _plain). A tab named for the
# subsystem's part of Crush ("Actuators", "Seals") is tried last: see `tab_candidates`.
TAB_ALIASES: dict[str, tuple[str, ...]] = {
    "Seals/Waterproofing": ("seals waterproofing", "seals and waterproofing", "waterproofing"),
    "Electronics Mounts": ("electronics mount", "electronics"),
    "Thrusters": ("thruster",),
    "Torpedo": ("torpedoes",),
}


class WriteError(Exception):
    """A write that didn't happen. The message is safe to show a member."""


# --- the entry text (pure) -----------------------------------------------------


@dataclass(frozen=True)
class Run:
    text: str
    bold: bool = False
    link: str | None = None


@dataclass
class Para:
    """One paragraph of an entry: runs of text, optionally bold or linked."""

    runs: list[Run]

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


@dataclass
class Entry:
    paragraphs: list[Para] = field(default_factory=list)

    @property
    def first_line(self) -> str:
        return self.paragraphs[0].text if self.paragraphs else ""


_SPACES = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _one_line(value: str, limit: int = MAX_FIELD_CHARS) -> str:
    """Text as one line: control characters out, whitespace collapsed, length capped."""
    return _SPACES.sub(" ", _CONTROL.sub("", value or "")).strip()[:limit]


def _sentence(value: str) -> str:
    value = value.rstrip()
    return value if not value or value[-1] in ".!?" else value + "."


def note_entry(
    note: WorkNote,
    specs: Sequence[NoteSpec] = (),
    photo_links: Sequence[str] = (),
    follows: str = "",
) -> Entry:
    """"Name · CODE: what. Why: ..." as one paragraph, then a line per accepted spec
    ("Spec: ... Source: Title", linked) and one line of photo links. `follows` names the
    earlier entry this one adds to ("12 Nov 2026")."""
    text = f"{_one_line(note.member_name, 80)} · {_one_line(note.task_code, 20)}: " + _sentence(
        _one_line(note.did)
    )
    why = _one_line(note.why)
    if why:
        text += f" Why: {_sentence(why)}"
    if follows:
        text += f" Adds to their entry of {_one_line(follows, 40)}."
    paragraphs = [Para([Run(text)])]
    for spec in specs:
        value = _one_line(spec.text, 300)
        if not value:
            continue
        unit = _one_line(spec.unit, 30)
        runs = [Run(f"Spec: {value}{(' ' + unit) if unit else ''}. Source: ")]
        title = _one_line(spec.source_title, 200) or "source"
        link = spec.source_link if spec.source_link.startswith("https://") else None
        runs.append(Run(title, link=link))
        paragraphs.append(Para(runs))
    links = [link for link in photo_links if link.startswith("https://")]
    if links:
        runs = [Run("Photos: ")]
        for number, link in enumerate(links, start=1):
            if number > 1:
                runs.append(Run(", "))
            runs.append(Run(f"Photo {number}", link=link))
        paragraphs.append(Para(runs))
    return Entry(paragraphs)


def answer_entry(
    decision: Decision, replaced: list[str], task_codes: list[str]
) -> Entry:
    """A recorded answer: the bold statement, then why, what it replaces, sources, and who
    recorded it. Lines with nothing to say are left out."""
    statement = f"{_one_line(decision.topic, 200)}: {_one_line(decision.decision)}"
    paragraphs = [Para([Run(statement, bold=True)])]
    why = _one_line(decision.rationale)
    if why:
        paragraphs.append(Para([Run(f"Why: {why}")]))
    old = [_one_line(text, 300) for text in replaced if _one_line(text)]
    if old:
        paragraphs.append(Para([Run("Replaces: " + "; ".join(old))]))
    runs: list[Run] = []
    for source in decision.sources or []:
        title = _one_line(str(source.get("title") or ""), 200) if isinstance(source, dict) else ""
        if not title:
            continue
        link = str(source.get("link") or "")
        runs.append(Run(", " if runs else "Sources: "))
        runs.append(Run(title, link=link if link.startswith("https://") else None))
    if runs:
        paragraphs.append(Para(runs))
    who = _one_line(decision.confirmed_by or decision.proposed_by, 80)
    where = "".join(f" · {_one_line(code, 20)}" for code in task_codes)
    paragraphs.append(Para([Run(f"Recorded by {who}{where}")]))
    return Entry(paragraphs)


# --- finding the place (pure) --------------------------------------------------


def _plain(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def tab_candidates(subsystem: str) -> list[str]:
    """Tab names to look for, best first: the subsystem's own name, other words for it,
    then the name of its part of Crush ("Actuators" for the Gripper)."""
    names = [subsystem, *TAB_ALIASES.get(subsystem, ())]
    for group in SUBSYSTEM_GROUPS:
        if subsystem in group["subsystems"]:
            names.append(group["label"])
    return list(dict.fromkeys(_plain(name) for name in names))


def flatten_tabs(tabs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every tab of a document, child tabs included, in reading order."""
    found: list[dict[str, Any]] = []
    for tab in tabs or []:
        found.append(tab)
        found.extend(flatten_tabs(tab.get("childTabs") or []))
    return found


def _title(tab: dict[str, Any]) -> str:
    return str((tab.get("tabProperties") or {}).get("title") or "")


def find_tab(
    document: dict[str, Any], subsystem: str
) -> tuple[dict[str, Any], bool]:
    """(the tab to write under, whether it is the General fallback). Raises WriteError
    when there is neither a tab for the subsystem nor a General tab."""
    tabs = flatten_tabs(document.get("tabs") or [])
    by_name: dict[str, dict[str, Any]] = {}
    for tab in tabs:
        by_name.setdefault(_plain(_title(tab)), tab)
    for name in tab_candidates(subsystem):
        if name in by_name:
            return by_name[name], False
    general = by_name.get(_plain(GENERAL_TAB))
    if general is not None:
        return general, True
    raise WriteError(
        f"The Crush document has no “{subsystem}” tab and no “{GENERAL_TAB}” tab. "
        f"Add a “{GENERAL_TAB}” tab to it, then retry."
    )


@dataclass
class _Existing:
    start: int
    end: int
    text: str
    style: str


def _paragraphs(tab: dict[str, Any]) -> list[_Existing]:
    content = ((tab.get("documentTab") or {}).get("body") or {}).get("content") or []
    found = []
    for element in content:
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        text = "".join(
            (part.get("textRun") or {}).get("content", "")
            for part in paragraph.get("elements") or []
        )
        found.append(
            _Existing(
                element["startIndex"],
                element["endIndex"],
                text.rstrip("\n"),
                (paragraph.get("paragraphStyle") or {}).get("namedStyleType", BODY_STYLE),
            )
        )
    return found


def _end_index(tab: dict[str, Any]) -> int:
    content = ((tab.get("documentTab") or {}).get("body") or {}).get("content") or []
    return max((el.get("endIndex", 1) for el in content), default=1)


def _heading_level(style: str) -> int | None:
    if style == "TITLE":
        return 0
    match = re.fullmatch(r"HEADING_(\d)", style)
    return int(match.group(1)) if match else None


@dataclass
class Plan:
    """What to send for one entry: the requests, or that it is already there."""

    requests: list[dict[str, Any]]
    already_there: bool = False


def plan_append(
    tab: dict[str, Any], heading: str, entry: Entry, check_existing: bool = True
) -> Plan:
    """The requests that add `entry` under `heading` in `tab` (the heading too, at the end
    of the tab, if the tab has none for that day). Pure: nothing is sent.

    An entry goes at the end of its day's section: just before the next heading of the same
    or a higher level, or at the end of the tab. Inserting there never touches existing text.
    With `check_existing` (a retry, where an earlier attempt may have reached the document
    without being recorded), an entry whose first line is already in the section isn't added.
    A first attempt never checks: two notes with the same words are two notes.
    """
    tab_id = (tab.get("tabProperties") or {}).get("tabId") or ""
    paras = _paragraphs(tab)
    tab_end = _end_index(tab) - 1  # before the tab's final newline

    heading_at = None
    for i, para in enumerate(paras):
        if para.text.strip() == heading:
            heading_at = i  # the last one: the newest if a day was typed in twice

    lines: list[tuple[str, str, list[Run]]] = []  # (text, paragraph style, runs)
    section_end: int | None = None  # where the next heading starts; None = end of the tab
    if heading_at is None:
        lines.append((heading, DATE_HEADING_STYLE, [Run(heading)]))
    else:
        level = _heading_level(paras[heading_at].style)
        level = 3 if level is None else level
        for later in paras[heading_at + 1 :]:
            their = _heading_level(later.style)
            if their is not None and their <= level:
                section_end = later.start
                break
            if check_existing and later.text.strip() == entry.first_line:
                return Plan([], already_there=True)
    lines.extend((p.text, BODY_STYLE, p.runs) for p in entry.paragraphs)

    if section_end is None:
        # At the end of the tab, before its final newline, which closes the last line.
        last_is_empty = bool(paras) and paras[-1].text == ""
        prefix = "" if last_is_empty else "\n"
        text = prefix + "\n".join(line[0] for line in lines)
        insert_at = tab_end
        position = insert_at + len(prefix)
    else:
        text = "\n".join(line[0] for line in lines) + "\n"
        insert_at = section_end
        position = insert_at

    def span(start: int, end: int) -> dict[str, Any]:
        return {"startIndex": start, "endIndex": end, "tabId": tab_id}

    requests: list[dict[str, Any]] = [
        {"insertText": {"location": {"index": insert_at, "tabId": tab_id}, "text": text}}
    ]
    for line_text, style, runs in lines:
        paragraph = span(position, position + len(line_text) + 1)
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": paragraph,
                    "paragraphStyle": {"namedStyleType": style},
                    "fields": "namedStyleType",
                }
            }
        )
        requests.append({"deleteParagraphBullets": {"range": paragraph}})
        if line_text:
            # The new text inherits the look of what is next to it (a heading's size and
            # colour, a link): reset every property to the paragraph style's own.
            requests.append(
                {
                    "updateTextStyle": {
                        "range": span(position, position + len(line_text)),
                        "textStyle": {},
                        "fields": RESET_TEXT_STYLE,
                    }
                }
            )
        at = position
        for run in runs:
            end = at + len(run.text)
            if run.bold and run.text:
                requests.append(_text_style(span(at, end), {"bold": True}, "bold"))
            if run.link and run.text:
                requests.append(_text_style(span(at, end), {"link": {"url": run.link}}, "link"))
            at = end
        position += len(line_text) + 1
    return Plan(requests)


def _text_style(span: dict[str, Any], style: dict[str, Any], fields: str) -> dict[str, Any]:
    return {"updateTextStyle": {"range": span, "textStyle": style, "fields": fields}}


# --- Google (the only calls that write) -----------------------------------------


def doc_id() -> str | None:
    return (os.environ.get(CRUSH_DOC_ENV_VAR) or "").strip() or None


def doc_link(tab_id: str | None = None) -> str | None:
    """A link to the Crush document (to its tab, when known), or None if it isn't set up."""
    found = doc_id()
    if not found:
        return None
    link = f"https://docs.google.com/document/d/{found}/edit"
    return f"{link}?tab={tab_id}" if tab_id else link


def build_docs() -> Any:
    """The Docs API client with write access, from the service-account environment."""
    credentials = drive._load_credentials([DOCS_WRITE_SCOPE])
    try:
        http = google_auth_httplib2.AuthorizedHttp(
            credentials, http=httplib2.Http(timeout=REQUEST_TIMEOUT_SECONDS)
        )
        return build("docs", "v1", http=http, cache_discovery=False)
    except Exception:  # noqa: BLE001  (initialization failures need one safe error)
        raise drive.DriveError("Google Docs could not be initialized.") from None


def _google_error(exc: HttpError) -> WriteError:
    """A safe message for a Docs failure. Never includes the response body."""
    status = getattr(exc.resp, "status", None)
    if status in (401, 403):
        return WriteError(
            "Google wouldn't let the app write to the Crush document. Share it with the "
            "app's service account as an editor."
        )
    if status == 404:
        return WriteError("Google couldn't find the Crush document. Check DECISIONS_DOC_ID.")
    if status == 429:
        return WriteError("Google Docs is busy (rate limit). Try again in a minute.")
    return WriteError(f"Google Docs didn't accept the write (error {status or 'unknown'}).")


def _revision_conflict(exc: HttpError) -> bool:
    return getattr(exc.resp, "status", None) in (400, 409) and "revision" in str(exc).casefold()


@dataclass
class Placed:
    tab_title: str
    tab_id: str | None
    used_general: bool


def append_entry(
    docs: Any,
    document_id: str,
    subsystem: str,
    day: date,
    entry: Entry,
    check_existing: bool = False,
) -> Placed:
    """Add the entry under its subsystem's tab and the day's heading. Raises WriteError.

    A write is sent with the revision its positions were computed from. If the document
    changed in between, it is read again and the positions recomputed.
    """
    heading = localtime.heading(day)
    for attempt in range(REVISION_TRIES):
        try:
            document = docs.documents().get(
                documentId=document_id, includeTabsContent=True
            ).execute(num_retries=2)
            tab, general = find_tab(document, subsystem)
            plan = plan_append(tab, heading, entry, check_existing)
            placed = Placed(
                _title(tab), (tab.get("tabProperties") or {}).get("tabId"), general
            )
            if plan.already_there:
                return placed
            body: dict[str, Any] = {"requests": plan.requests}
            if document.get("revisionId"):
                body["writeControl"] = {"requiredRevisionId": document["revisionId"]}
            docs.documents().batchUpdate(documentId=document_id, body=body).execute(
                num_retries=2
            )
            return placed
        except HttpError as exc:
            if _revision_conflict(exc) and attempt < REVISION_TRIES - 1:
                continue
            raise _google_error(exc) from None
    raise WriteError("The Crush document kept changing while it was being written to. Retry.")


# --- one write per entry ---------------------------------------------------------


def _claim(session: Session, kind: str, ref_id: int) -> DriveWrite | None:
    """The entry's row if this caller may write now; None if it is already written or
    another request is writing it."""
    row = session.exec(
        select(DriveWrite).where(DriveWrite.kind == kind, DriveWrite.ref_id == ref_id)
    ).first()
    if row is None:
        row = DriveWrite(kind=kind, ref_id=ref_id)
        session.add(row)
        try:
            session.commit()
        except IntegrityError:  # a second request made it first
            session.rollback()
            row = session.exec(
                select(DriveWrite).where(DriveWrite.kind == kind, DriveWrite.ref_id == ref_id)
            ).one()
    if row.status == "written":
        return None
    now = utc_now()
    claimed = session.execute(
        update(DriveWrite)
        .where(
            col(DriveWrite.id) == row.id,
            col(DriveWrite.status).in_(("pending", "failed"))
            | (
                (col(DriveWrite.status) == "writing")
                & (col(DriveWrite.updated_at) < now - STALE_WRITE_AFTER)
            ),
        )
        .values(status="writing", attempts=DriveWrite.attempts + 1, updated_at=now)
    )
    session.commit()
    if claimed.rowcount != 1:
        return None
    session.refresh(row)
    return row


def _write(
    session: Session,
    kind: str,
    ref_id: int,
    subsystem: str,
    day: date,
    entry: Entry,
    docs: Any | None,
) -> DriveWrite:
    """Write one entry once; the row says how it went. Never raises."""
    row = _claim(session, kind, ref_id)
    if row is None:
        return session.exec(
            select(DriveWrite).where(DriveWrite.kind == kind, DriveWrite.ref_id == ref_id)
        ).one()
    error = None
    placed: Placed | None = None
    try:
        document_id = doc_id()
        if not document_id:
            raise WriteError(NOT_CONFIGURED)
        placed = append_entry(
            docs or build_docs(), document_id, subsystem, day, entry,
            check_existing=row.attempts > 1,
        )
    except WriteError as exc:
        error = str(exc)
    except drive.DriveError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001  (a write must never break the page)
        log.error("Could not write to the Crush document (%s)", type(exc).__name__)
        error = "Couldn't reach Google Docs. The entry is saved here; try the write again."
    now = utc_now()
    row.updated_at = now
    if placed is not None:
        row.status, row.error, row.written_at = "written", None, now
        row.tab_title, row.tab_id, row.used_general = (
            placed.tab_title,
            placed.tab_id,
            placed.used_general,
        )
    else:
        row.status, row.error = "failed", error
    session.add(row)
    session.commit()
    return row


def accepted_specs(session: Session, note: WorkNote) -> list[NoteSpec]:
    return list(
        session.exec(
            select(NoteSpec)
            .where(NoteSpec.note_id == note.id, col(NoteSpec.accepted).is_(True))
            .order_by(NoteSpec.position, NoteSpec.id)
        ).all()
    )


def _photos(session: Session, note: WorkNote) -> list[NotePhoto]:
    """The entry's photos without their bytes."""
    return list(
        session.exec(
            select(NotePhoto)
            .options(defer(NotePhoto.data))
            .where(NotePhoto.note_id == note.id)
            .order_by(NotePhoto.position, NotePhoto.id)
        ).all()
    )


def write_note(session: Session, note: WorkNote, docs: Any | None = None) -> DriveWrite:
    """Append a notebook entry to the Crush document (once), with the specs the member
    accepted and a link to each photo that reached Drive. Never raises."""
    follows = ""
    if note.refers_to_id:
        earlier = session.get(WorkNote, note.refers_to_id)
        if earlier is not None:
            follows = localtime.heading(localtime.local_date(earlier.posted_at))
    links = [p.drive_link for p in _photos(session, note) if p.drive_link]
    return _write(
        session,
        "note",
        note.id,
        note.subsystem,
        localtime.local_date(note.posted_at),
        note_entry(note, accepted_specs(session, note), links, follows),
        docs,
    )


# --- photos: uploaded to the "Notebook photos" folder before the entry is written ----


def write_folder_id() -> str | None:
    return (os.environ.get(WRITE_FOLDER_ENV_VAR) or "").strip() or None


def build_drive() -> Any:
    """The Drive API client that may create files this app makes (`drive.file`)."""
    credentials = drive._load_credentials([DRIVE_FILE_SCOPE])
    try:
        http = google_auth_httplib2.AuthorizedHttp(
            credentials, http=httplib2.Http(timeout=REQUEST_TIMEOUT_SECONDS)
        )
        return build("drive", "v3", http=http, cache_discovery=False)
    except Exception:  # noqa: BLE001  (initialization failures need one safe error)
        raise drive.DriveError("Google Drive could not be initialized.") from None


def _photo_folder(service: Any, parent_id: str) -> str:
    """The id of the "Notebook photos" folder inside the writable folder, made if it isn't
    there yet. Raises WriteError."""
    escaped = PHOTO_FOLDER_NAME.replace("'", "\\'")
    try:
        found = (
            service.files()
            .list(
                q=(
                    f"name = '{escaped}' and '{parent_id}' in parents and "
                    f"mimeType = '{FOLDER_MIME}' and trashed = false"
                ),
                fields="files(id)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute(num_retries=2)
        )
        if found.get("files"):
            return str(found["files"][0]["id"])
        made = (
            service.files()
            .create(
                body={"name": PHOTO_FOLDER_NAME, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                fields="id",
                supportsAllDrives=True,
            )
            .execute(num_retries=2)
        )
        return str(made["id"])
    except HttpError as exc:
        raise _photo_error(exc) from None


def _photo_error(exc: HttpError) -> WriteError:
    status = getattr(exc.resp, "status", None)
    if status in (401, 403):
        return WriteError(
            "Google wouldn't let the app add photos to the Drive folder. Share the folder "
            "with the app's service account as an editor."
        )
    if status == 404:
        return WriteError("Google couldn't find the Drive folder. Check DRIVE_WRITE_FOLDER_ID.")
    if status == 429:
        return WriteError("Google Drive is busy (rate limit). Try again in a minute.")
    return WriteError(f"Google Drive didn't accept the photo (error {status or 'unknown'}).")


def _upload(service: Any, folder_id: str, name: str, data: bytes) -> tuple[str, str]:
    """(file id, link) of a JPEG uploaded to the folder. Raises WriteError."""
    try:
        made = (
            service.files()
            .create(
                body={"name": name, "parents": [folder_id]},
                media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype="image/jpeg"),
                fields="id, webViewLink",
                supportsAllDrives=True,
            )
            .execute(num_retries=2)
        )
    except HttpError as exc:
        raise _photo_error(exc) from None
    link = str(made.get("webViewLink") or "")
    if not link.startswith("https://"):
        raise WriteError("Google Drive didn't give a link for the photo.")
    return str(made["id"]), link


@dataclass
class PhotoUpload:
    """How the photos of one entry went: `done` have a Drive link, `stayed` don't."""

    done: int = 0
    stayed: int = 0
    error: str | None = None


def upload_photos(
    session: Session, note: WorkNote, service: Any | None = None
) -> PhotoUpload:
    """Upload the entry's photos that aren't in Drive yet, each once (a `drive_write` row of
    kind "photo"). Stops at the first failure, since the rest would fail the same way. Never
    raises: a photo that can't be uploaded stays in the app and the entry is published
    anyway."""
    photos = _photos(session, note)
    result = PhotoUpload(done=sum(1 for p in photos if p.drive_link))
    todo = [p for p in photos if not p.drive_link]
    if not todo:
        return result
    result.stayed = len(todo)
    parent = write_folder_id()
    try:
        if not parent:
            raise WriteError(
                "No Drive folder is set up for photos (DRIVE_WRITE_FOLDER_ID is missing)."
            )
        service = service or build_drive()
        folder = _photo_folder(service, parent)
    except (WriteError, drive.DriveError) as exc:
        result.error = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001  (a photo upload must never break publishing)
        log.error("Could not reach Google Drive for photos (%s)", type(exc).__name__)
        result.error = "Couldn't reach Google Drive."
        return result
    for photo in todo:
        row = _claim(session, "photo", photo.id)
        if row is None:  # written already, or another request has it
            continue
        error = None
        try:
            data = session.exec(select(NotePhoto.data).where(NotePhoto.id == photo.id)).one()
            name = f"{note.task_code}-entry{note.id}-photo{photo.position + 1}.jpg"
            file_id, link = _upload(service, folder, name, data)
        except WriteError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not upload a notebook photo (%s)", type(exc).__name__)
            error = "Couldn't reach Google Drive."
        now = utc_now()
        row.updated_at = now
        if error is None:
            row.status, row.error, row.written_at = "written", None, now
            session.add(row)
            session.execute(
                update(NotePhoto)
                .where(col(NotePhoto.id) == photo.id)
                .values(drive_file_id=file_id, drive_link=link)
            )
            session.commit()
            result.done += 1
            result.stayed -= 1
        else:
            row.status, row.error = "failed", error
            session.add(row)
            session.commit()
            result.error = error
            break
    return result


@dataclass
class Published:
    """What publishing an entry did: the write of its text, and how its photos went."""

    write: DriveWrite
    photos: PhotoUpload


def publish_note(
    session: Session,
    note: WorkNote,
    docs: Any | None = None,
    service: Any | None = None,
) -> Published:
    """Publish an entry: upload its photos, then append it to the Crush document once.

    A failed photo upload never stops the text. An entry whose text is already in the
    document is left as it is (its photos can't be added to what is already written).
    Never raises."""
    existing = session.exec(
        select(DriveWrite).where(DriveWrite.kind == "note", DriveWrite.ref_id == note.id)
    ).first()
    if existing is not None and existing.status == "written":
        photos = _photos(session, note)
        done = sum(1 for p in photos if p.drive_link)
        return Published(existing, PhotoUpload(done=done, stayed=len(photos) - done))
    try:
        uploaded = upload_photos(session, note, service)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not upload photos (%s)", type(exc).__name__)
        photos = _photos(session, note)
        uploaded = PhotoUpload(stayed=len(photos), error="Couldn't reach Google Drive.")
    return Published(write_note(session, note, docs), uploaded)


def write_answer(session: Session, decision: Decision, docs: Any | None = None) -> DriveWrite:
    """Append a recorded answer to the Crush document (once). Never raises."""
    replaced = [
        d.decision
        for d in session.exec(
            select(Decision).where(Decision.superseded_by_id == decision.id)
        ).all()
    ]
    codes = list(
        session.exec(
            select(Task.code)
            .join(DecisionTask, col(DecisionTask.task_id) == col(Task.id))
            .where(DecisionTask.decision_id == decision.id)
            .order_by(Task.code)
        ).all()
    )
    return _write(
        session,
        "answer",
        decision.id,
        decision.subsystem,
        localtime.local_date(decision.confirmed_at or decision.created_at),
        answer_entry(decision, replaced, codes),
        docs,
    )


def retry(session: Session, kind: str, ref_id: int, docs: Any | None = None) -> DriveWrite | None:
    """Try a failed entry's write again. None if there is no such entry (or an answer that
    isn't recorded: a suggested or replaced one is never written)."""
    if kind == "note":
        note = session.get(WorkNote, ref_id)
        if note is None or note.state != "published":  # a draft is never written
            return None
        return publish_note(session, note, docs).write
    decision = session.get(Decision, ref_id)
    if decision is None or decision.status != "confirmed":  # only a recorded answer is written
        return None
    return write_answer(session, decision, docs)


def status_text(write: DriveWrite | None, subsystem: str) -> str:
    """One line for a member: where an entry stands in the Crush document."""
    if write is None or write.status in ("pending", "writing"):
        return "Not written to the Crush document yet."
    if write.status == "written":
        where = f"the {write.tab_title} tab" if write.tab_title else "the Crush document"
        line = f"Written to {where} of the Crush document."
        if write.used_general:
            line += f" There is no {subsystem} tab, so it went under {GENERAL_TAB}."
        return line
    return write.error or "It couldn't be written to the Crush document."
