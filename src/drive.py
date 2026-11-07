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

    def search(self, query: str, limit: int = 10) -> list[DriveSearchResult]:
        """Search file names and indexed text, with newest results first."""
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
                        "nextPageToken,files(id,name,webViewLink,mimeType,modifiedTime)"
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

        results: list[DriveSearchResult] = []
        for file in files:
            file_id = file.get("id", "")
            results.append(
                {
                    "title": file.get("name", "Untitled"),
                    "link": file.get("webViewLink")
                    or f"https://drive.google.com/open?id={file_id}",
                    "mime_type": file.get("mimeType", "application/octet-stream"),
                    "modified_time": file.get("modifiedTime", ""),
                }
            )
        results.sort(key=lambda result: result["modified_time"], reverse=True)
        return results[:limit]

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
