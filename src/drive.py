"""Read-only access to the team's shared Google Drive."""

import json
import os
from typing import Any, TypedDict

from google.oauth2 import service_account
from googleapiclient.discovery import build

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
PLAIN_TEXT_MIME_TYPE = "text/plain"


class DriveError(RuntimeError):
    """A safe-to-display Google Drive configuration or request failure."""


class DriveSearchResult(TypedDict):
    """The Drive metadata callers need to show a search result."""

    title: str
    link: str
    mime_type: str
    modified_time: str
    folder_path: str
    locations: list["DriveFileLocation"]


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

    def search(self, query: str, limit: int = 10) -> list[DriveSearchResult]:
        """Search names and text, grouping names and ranking name matches first."""
        if limit <= 0:
            return []

        escaped_query = _escape_query_value(query)
        drive_query = (
            "trashed = false and "
            f"(name contains '{escaped_query}' or fullText contains '{escaped_query}')"
        )

        files: list[dict[str, Any]] = []
        page_token: str | None = None
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
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except Exception:  # noqa: BLE001 (all API failures become safe DriveErrors)
            raise DriveError(
                "Google Drive search failed. Check network access and shared-drive "
                "permissions."
            ) from None

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
                    "name_match": query.casefold() in title.casefold(),
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


def search(query: str, limit: int = 10) -> list[DriveSearchResult]:
    """Search the configured shared drive."""
    return DriveClient().search(query, limit)


def read_text(file_id: str) -> str:
    """Read a Google Doc from the configured shared drive as plain text."""
    return DriveClient().read_text(file_id)
