"""Read-only access to the team's shared Google Drive."""

import json
import os
import re
from typing import Any, TypedDict

from google.oauth2 import service_account
from googleapiclient.discovery import build

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
PLAIN_TEXT_MIME_TYPE = "text/plain"
DOCX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# The file types drive.search can filter on. CAD files are matched by extension
# (they have no Google mime type and no searchable text); the rest by mime type.
CAD_EXTENSIONS = (".SLDASM", ".SLDPRT", ".SLDDRW", ".STEP", ".STP")
FILE_TYPE_MIME_TYPES: dict[str, tuple[str, ...]] = {
    "sheets": (GOOGLE_SHEET_MIME_TYPE, XLSX_MIME_TYPE, "text/csv"),
    "docs": (GOOGLE_DOC_MIME_TYPE, DOCX_MIME_TYPE),
    "pdf": ("application/pdf",),
}
FILE_TYPES = ("cad", "sheets", "docs", "pdf")

MAX_TYPED_SEARCH_PAGES = 3  # a type filter can match thousands of files
MAX_LIST_PAGES = 2
DEFAULT_LIST_LIMIT = 100


class DriveError(RuntimeError):
    """A safe-to-display Google Drive configuration or request failure."""


class FolderNotFound(DriveError):
    """A folder path or id matched nothing (Drive itself worked)."""


class DriveSearchResult(TypedDict):
    """The Drive metadata callers need to show a search result."""

    title: str
    link: str
    mime_type: str
    modified_time: str
    folder_path: str
    locations: list["DriveFileLocation"]


class FolderEntry(TypedDict):
    """One child of a folder."""

    id: str
    name: str
    kind: str  # folder, doc, sheet, cad, pdf, other
    modified_time: str
    link: str


class FolderCandidate(TypedDict):
    """One folder a path could mean."""

    id: str
    folder_path: str
    modified_time: str


class FolderListing(TypedDict):
    """What drive.list_folder returns: the children, or the folders to pick from."""

    folder_path: str
    folder_id: str
    entries: list[FolderEntry]
    total: int
    candidates: list[FolderCandidate]


class DriveFileLocation(TypedDict):
    """One file in a group of Drive files that share a name."""

    link: str
    mime_type: str
    modified_time: str
    folder_path: str


def _load_credentials():
    """Load service-account credentials without exposing their contents."""
    credential_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    credential_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

    try:
        if credential_json:
            credential_info = json.loads(credential_json)
            if not isinstance(credential_info, dict):
                raise ValueError("service-account JSON must be an object")
            return service_account.Credentials.from_service_account_info(
                credential_info,
                scopes=[DRIVE_READONLY_SCOPE],
            )
        if credential_path:
            return service_account.Credentials.from_service_account_file(
                credential_path,
                scopes=[DRIVE_READONLY_SCOPE],
            )
    except Exception:  # noqa: BLE001 (credential libraries raise varied errors)
        raise DriveError("Google Drive credentials could not be loaded.") from None

    raise DriveError(
        "Google Drive credentials are not configured. Set "
        "GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS."
    )


def _build_service():
    try:
        return build(
            "drive",
            "v3",
            credentials=_load_credentials(),
            cache_discovery=False,
        )
    except DriveError:
        raise
    except Exception:  # noqa: BLE001 (initialization failures need one safe error)
        raise DriveError("Google Drive could not be initialized.") from None


def _escape_query_value(value: str) -> str:
    """Escape a string literal for the Drive API's query language."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _file_kind(name: str, mime_type: str) -> str:
    """A short, friendly kind for a file: folder, doc, sheet, cad, pdf or other."""
    if mime_type == GOOGLE_FOLDER_MIME_TYPE:
        return "folder"
    if name.upper().endswith(CAD_EXTENSIONS):
        return "cad"
    for kind, mime_types in FILE_TYPE_MIME_TYPES.items():
        if mime_type in mime_types:
            return kind.rstrip("s")  # sheets -> sheet, docs -> doc
    return "other"


_FOLDER_ID_PATTERNS = (
    re.compile(r"/folders/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
)
_BARE_ID = re.compile(r"[A-Za-z0-9_-]{20,}")


def _join_path(parent_path: str, name: str) -> str:
    """'A / B' + 'C' -> 'A / B / C'; a root folder is just its name."""
    return f"{parent_path} / {name}" if parent_path not in ("", "/") else name


def folder_id_from(value: str) -> str | None:
    """A Drive folder id from a folder link or a bare id; None for a path."""
    value = value.strip()
    for pattern in _FOLDER_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return value if _BARE_ID.fullmatch(value) else None


class DriveClient:
    """Small, read-only wrapper around a Google Drive v3 service."""

    def __init__(self, service: Any | None = None):
        self._service = service if service is not None else _build_service()

    def _folder_path(
        self,
        parent_ids: list[str],
        folder_cache: dict[str, dict[str, Any]],
    ) -> str:
        """Resolve a file's first parent chain into a readable folder path."""
        if not parent_ids:
            return "/"

        folder_names: list[str] = []
        folder_id = parent_ids[0]
        visited: set[str] = set()
        try:
            while folder_id and folder_id not in visited:
                visited.add(folder_id)
                if folder_id not in folder_cache:
                    folder_cache[folder_id] = (
                        self._service.files()
                        .get(
                            fileId=folder_id,
                            fields="id,name,parents,mimeType",
                            supportsAllDrives=True,
                        )
                        .execute()
                    )
                folder = folder_cache[folder_id]
                folder_names.append(folder.get("name", "Untitled folder"))
                parents = folder.get("parents", [])
                folder_id = parents[0] if parents else ""
        except Exception:  # noqa: BLE001 (all API failures become safe DriveErrors)
            raise DriveError(
                "Google Drive could not resolve a folder path. Check network access "
                "and shared-drive permissions."
            ) from None

        return " / ".join(reversed(folder_names)) or "/"

    @staticmethod
    def _search_query(query: str, file_type: str | None) -> str:
        """The Drive query for a search. Plain searches look at names and text."""
        clauses = ["trashed = false"]
        words = query.split()
        if file_type == "cad":
            # CAD files have no searchable text; match the extension and the name.
            extensions = " or ".join(
                f"name contains '{extension.lstrip('.')}'"
                for extension in CAD_EXTENSIONS
            )
            clauses.append(f"({extensions})")
            clauses.extend(
                f"name contains '{_escape_query_value(word)}'" for word in words
            )
        else:
            if words:
                escaped = _escape_query_value(query)
                clauses.append(
                    f"(name contains '{escaped}' or fullText contains '{escaped}')"
                )
            if file_type:
                mime_types = " or ".join(
                    f"mimeType = '{mime}'" for mime in FILE_TYPE_MIME_TYPES[file_type]
                )
                clauses.append(f"({mime_types})")
        return " and ".join(clauses)

    def search(
        self, query: str, limit: int = 10, file_type: str | None = None
    ) -> list[DriveSearchResult]:
        """Search names and text, grouping names and ranking name matches first.

        `file_type` (cad, sheets, docs, pdf) keeps only that kind of file. With a
        type, `query` may be empty: that lists the newest files of the type.
        """
        if limit <= 0:
            return []
        if file_type is not None and file_type not in FILE_TYPES:
            raise DriveError(
                f"Unknown file type {file_type!r}. Use one of: {', '.join(FILE_TYPES)}."
            )

        drive_query = self._search_query(query, file_type)
        max_pages = MAX_TYPED_SEARCH_PAGES if file_type else None

        files: list[dict[str, Any]] = []
        page_token: str | None = None
        pages = 0
        try:
            while True:
                request_parameters: dict[str, Any] = {
                    "q": drive_query,
                    "pageSize": 1000,
                    "fields": (
                        "nextPageToken,"
                        "files(id,name,webViewLink,mimeType,modifiedTime,parents)"
                    ),
                    "supportsAllDrives": True,
                    "includeItemsFromAllDrives": True,
                    "corpora": "allDrives",
                }
                if page_token:
                    request_parameters["pageToken"] = page_token

                response = self._service.files().list(**request_parameters).execute()
                files.extend(response.get("files", []))
                pages += 1
                page_token = response.get("nextPageToken")
                if not page_token or (max_pages and pages >= max_pages):
                    break
        except Exception:  # noqa: BLE001 (all API failures become safe DriveErrors)
            raise DriveError(
                "Google Drive search failed. Check network access and shared-drive "
                "permissions."
            ) from None

        if file_type == "cad":
            # Drive matches name words, not suffixes, so make the result exact.
            files = [
                file
                for file in files
                if file.get("name", "").upper().endswith(CAD_EXTENSIONS)
            ]
        words = query.casefold().split()

        folder_cache: dict[str, dict[str, Any]] = {}
        grouped_files: dict[str, dict[str, Any]] = {}
        for file in files:
            file_id = file.get("id", "")
            title = file.get("name", "Untitled")
            location: DriveFileLocation = {
                "link": file.get("webViewLink")
                or f"https://drive.google.com/open?id={file_id}",
                "mime_type": file.get("mimeType", "application/octet-stream"),
                "modified_time": file.get("modifiedTime", ""),
                "folder_path": self._folder_path(file.get("parents", []), folder_cache),
            }
            group = grouped_files.setdefault(
                title.casefold(),
                {
                    "title": title,
                    "name_match": all(word in title.casefold() for word in words),
                    "locations": [],
                },
            )
            group["locations"].append(location)

        ranked_results: list[tuple[bool, str, DriveSearchResult]] = []
        for group in grouped_files.values():
            locations = group["locations"]
            locations.sort(key=lambda location: location["modified_time"], reverse=True)
            newest = locations[0]
            result: DriveSearchResult = {
                "title": group["title"],
                "link": newest["link"],
                "mime_type": newest["mime_type"],
                "modified_time": newest["modified_time"],
                "folder_path": newest["folder_path"],
                "locations": locations,
            }
            ranked_results.append(
                (group["name_match"], newest["modified_time"], result)
            )

        ranked_results.sort(key=lambda ranked: (ranked[0], ranked[1]), reverse=True)
        return [ranked[2] for ranked in ranked_results[:limit]]

    def _list_request(self, query: str, fields: str, max_pages: int) -> list[dict]:
        """Run a files.list query (shared-drive flags included), a few pages deep."""
        found: list[dict] = []
        page_token: str | None = None
        for _ in range(max_pages):
            parameters: dict[str, Any] = {
                "q": query,
                "pageSize": 1000,
                "fields": f"nextPageToken,files({fields})",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
                "corpora": "allDrives",
            }
            if page_token:
                parameters["pageToken"] = page_token
            response = self._service.files().list(**parameters).execute()
            found.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return found

    def _find_folders(self, path: str) -> list[FolderCandidate]:
        """Folders whose full path ends with `path` (e.g. 'Mechanical/CAD')."""
        segments = [part.strip() for part in path.replace("\\", "/").split("/")]
        segments = [part for part in segments if part]
        if not segments:
            raise FolderNotFound("Give a folder path like 'Mechanical/CAD' or an id.")

        last = _escape_query_value(segments[-1])
        try:
            folders = self._list_request(
                f"trashed = false and mimeType = '{GOOGLE_FOLDER_MIME_TYPE}' "
                f"and name contains '{last}'",
                "id,name,parents,modifiedTime",
                MAX_LIST_PAGES,
            )
        except Exception:  # noqa: BLE001 (all API failures become safe DriveErrors)
            raise DriveError(
                "Google Drive could not look up that folder. Check network access "
                "and shared-drive permissions."
            ) from None

        wanted = [segment.casefold() for segment in segments]
        folder_cache: dict[str, dict[str, Any]] = {}
        candidates: list[FolderCandidate] = []
        for folder in folders:
            if folder.get("name", "").casefold() != wanted[-1]:
                continue
            full_path = self._folder_path(folder.get("parents", []), folder_cache)
            chain = [part.strip().casefold() for part in full_path.split(" / ")]
            chain.append(wanted[-1])  # "/" (no parent) can't equal a wanted name
            if chain[-len(wanted) :] != wanted:
                continue
            candidates.append(
                {
                    "id": folder.get("id", ""),
                    "folder_path": _join_path(full_path, folder.get("name", "")),
                    "modified_time": folder.get("modifiedTime", ""),
                }
            )
        candidates.sort(key=lambda found: found["modified_time"], reverse=True)
        return candidates

    def list_folder(
        self, folder: str, limit: int = DEFAULT_LIST_LIMIT
    ) -> FolderListing:
        """List a folder's children (folders first, then newest) by path or id.

        If a path matches several folders, `entries` is empty and `candidates`
        holds each match's id and full path, so the caller can pick one.
        """
        folder_id = folder_id_from(folder)
        candidates: list[FolderCandidate] = []
        folder_path = ""
        if folder_id is None:
            candidates = self._find_folders(folder)
            if not candidates:
                raise FolderNotFound(f"No folder matches {folder.strip()!r}.")
            if len(candidates) > 1:
                return {
                    "folder_path": "",
                    "folder_id": "",
                    "entries": [],
                    "total": 0,
                    "candidates": candidates,
                }
            folder_id = candidates[0]["id"]
            folder_path = candidates[0]["folder_path"]

        try:
            if not folder_path:
                info = (
                    self._service.files()
                    .get(
                        fileId=folder_id,
                        fields="id,name,parents,mimeType",
                        supportsAllDrives=True,
                    )
                    .execute()
                )
                if info.get("mimeType") != GOOGLE_FOLDER_MIME_TYPE:
                    raise FolderNotFound("That id is not a folder.")
                parent_path = self._folder_path(info.get("parents", []), {})
                folder_path = _join_path(parent_path, info.get("name", ""))
            children = self._list_request(
                f"trashed = false and '{_escape_query_value(folder_id)}' in parents",
                "id,name,webViewLink,mimeType,modifiedTime",
                MAX_LIST_PAGES,
            )
        except DriveError:
            raise
        except Exception:  # noqa: BLE001 (all API failures become safe DriveErrors)
            raise DriveError(
                "Google Drive could not list that folder. Check network access and "
                "shared-drive permissions."
            ) from None

        entries: list[FolderEntry] = [
            {
                "id": child.get("id", ""),
                "name": child.get("name", "Untitled"),
                "kind": _file_kind(child.get("name", ""), child.get("mimeType", "")),
                "modified_time": child.get("modifiedTime", ""),
                "link": child.get("webViewLink")
                or f"https://drive.google.com/open?id={child.get('id', '')}",
            }
            for child in children
        ]
        # Newest first, then folders ahead of files (sorts are stable).
        entries.sort(key=lambda entry: entry["modified_time"], reverse=True)
        entries.sort(key=lambda entry: entry["kind"] != "folder")
        return {
            "folder_path": folder_path,
            "folder_id": folder_id,
            "entries": entries[: max(limit, 0)],
            "total": len(entries),
            "candidates": [],
        }

    def read_text(self, file_id: str) -> str:
        """Export a Google Doc as text, or explain why a type is unsupported."""
        try:
            metadata = (
                self._service.files()
                .get(
                    fileId=file_id,
                    fields="mimeType",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except Exception:  # noqa: BLE001 (all API failures become safe DriveErrors)
            raise DriveError(
                "Google Drive could not inspect this file. Check network access and "
                "shared-drive permissions."
            ) from None

        mime_type = metadata.get("mimeType", "unknown")
        if mime_type != GOOGLE_DOC_MIME_TYPE:
            return (
                f"Can't read this file type ({mime_type}). "
                "Only Google Docs are supported."
            )

        try:
            content = (
                self._service.files()
                .export_media(fileId=file_id, mimeType=PLAIN_TEXT_MIME_TYPE)
                .execute()
            )
            if isinstance(content, bytes):
                return content.decode("utf-8")
            return str(content)
        except Exception:  # noqa: BLE001 (all API failures become safe DriveErrors)
            raise DriveError(
                "Google Drive could not read this document. Check network access and "
                "shared-drive permissions."
            ) from None


def search(
    query: str, limit: int = 10, file_type: str | None = None
) -> list[DriveSearchResult]:
    """Search the configured shared drive, optionally for one file type."""
    return DriveClient().search(query, limit, file_type)


def list_folder(folder: str, limit: int = DEFAULT_LIST_LIMIT) -> FolderListing:
    """List a folder's children by folder path or id."""
    return DriveClient().list_folder(folder, limit)


def read_text(file_id: str) -> str:
    """Read a Google Doc from the configured shared drive as plain text."""
    return DriveClient().read_text(file_id)
