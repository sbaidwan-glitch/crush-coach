from unittest.mock import Mock

import pytest

import drive


@pytest.fixture
def files_api():
    service = Mock()
    files = service.files.return_value
    return service, files


def test_searches_shared_drive_and_maps_results(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {
        "files": [
            {
                "id": "made-up-file-id",
                "name": "Latch design",
                "webViewLink": "https://example.test/latch",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-11-06T20:15:00.000Z",
            }
        ]
    }

    results = drive.DriveClient(service=service).search("latch")

    assert results == [
        {
            "title": "Latch design",
            "link": "https://example.test/latch",
            "mime_type": "application/vnd.google-apps.document",
            "modified_time": "2026-11-06T20:15:00.000Z",
        }
    ]
    files.list.assert_called_once_with(
        q=("trashed = false and (name contains 'latch' or fullText contains 'latch')"),
        pageSize=1000,
        fields="nextPageToken,files(id,name,webViewLink,mimeType,modifiedTime)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    )


def test_search_uses_all_drives_corpus_without_drive_id(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {"files": []}

    drive.DriveClient(service=service).search("latch")

    parameters = files.list.call_args.kwargs
    assert parameters["corpora"] == "allDrives"
    assert "driveId" not in parameters


def test_search_sorts_newest_first_without_api_order_by(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {
        "files": [
            {
                "id": "older-file",
                "name": "Older latch notes",
                "webViewLink": "https://example.test/older",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-10-01T00:00:00.000Z",
            },
            {
                "id": "newer-file",
                "name": "Newer latch notes",
                "webViewLink": "https://example.test/newer",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-11-01T00:00:00.000Z",
            },
        ]
    }

    results = drive.DriveClient(service=service).search("latch")

    assert [result["title"] for result in results] == [
        "Newer latch notes",
        "Older latch notes",
    ]
    assert "orderBy" not in files.list.call_args.kwargs


def test_search_escapes_drive_query_literals(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {"files": []}

    drive.DriveClient(service=service).search("Samara's latch\\notes")

    query = files.list.call_args.kwargs["q"]
    assert "Samara\\'s latch\\\\notes" in query
    assert "trashed = false" in query


def test_search_applies_limit_and_returns_empty_for_nonpositive_limit(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {
        "files": [
            {
                "id": f"file-{number}",
                "name": f"Latch notes {number}",
                "mimeType": "text/plain",
                "modifiedTime": f"2026-11-0{number}T00:00:00.000Z",
            }
            for number in range(1, 5)
        ]
    }
    client = drive.DriveClient(service=service)

    assert len(client.search("latch", limit=3)) == 3
    assert files.list.call_args.kwargs["pageSize"] == 1000
    files.reset_mock()

    assert client.search("latch", limit=0) == []
    files.list.assert_not_called()


def test_search_falls_back_to_a_drive_link(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {
        "files": [
            {
                "id": "made-up-file-id",
                "name": "Latch notes",
                "mimeType": "text/plain",
                "modifiedTime": "2026-11-01T00:00:00.000Z",
            }
        ]
    }

    result = drive.DriveClient(service=service).search("latch")[0]

    assert result["link"] == "https://drive.google.com/open?id=made-up-file-id"


def test_read_text_exports_google_doc(files_api):
    service, files = files_api
    files.get.return_value.execute.return_value = {
        "mimeType": drive.GOOGLE_DOC_MIME_TYPE
    }
    files.export_media.return_value.execute.return_value = b"Made-up latch notes\n"

    text = drive.DriveClient(service=service).read_text("made-up-file-id")

    assert text == "Made-up latch notes\n"
    files.get.assert_called_once_with(
        fileId="made-up-file-id",
        fields="mimeType",
        supportsAllDrives=True,
    )
    files.export_media.assert_called_once_with(
        fileId="made-up-file-id",
        mimeType="text/plain",
    )


def test_read_text_explains_unsupported_type(files_api):
    service, files = files_api
    files.get.return_value.execute.return_value = {"mimeType": "application/pdf"}

    result = drive.DriveClient(service=service).read_text("made-up-file-id")

    assert result == (
        "Can't read this file type (application/pdf). Only Google Docs are supported."
    )
    files.export_media.assert_not_called()


def test_json_credentials_take_precedence_and_use_readonly_scope(monkeypatch):
    json_loader = Mock(return_value=object())
    file_loader = Mock(return_value=object())
    monkeypatch.setattr(
        drive.service_account.Credentials,
        "from_service_account_info",
        json_loader,
    )
    monkeypatch.setattr(
        drive.service_account.Credentials,
        "from_service_account_file",
        file_loader,
    )
    monkeypatch.setenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        '{"type": "service_account", "project_id": "made-up"}',
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/made-up.json")

    credentials = drive._load_credentials()

    assert credentials is json_loader.return_value
    json_loader.assert_called_once_with(
        {"type": "service_account", "project_id": "made-up"},
        scopes=[drive.DRIVE_READONLY_SCOPE],
    )
    file_loader.assert_not_called()


def test_file_credentials_use_readonly_scope(monkeypatch):
    file_loader = Mock(return_value=object())
    monkeypatch.setattr(
        drive.service_account.Credentials,
        "from_service_account_file",
        file_loader,
    )
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/made-up.json")

    credentials = drive._load_credentials()

    assert credentials is file_loader.return_value
    file_loader.assert_called_once_with(
        "/tmp/made-up.json",
        scopes=[drive.DRIVE_READONLY_SCOPE],
    )


def test_missing_credentials_raise_clear_drive_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with pytest.raises(drive.DriveError, match="credentials are not configured"):
        drive.DriveClient()


def test_credential_failure_does_not_expose_secret(monkeypatch):
    secret = "DO-NOT-PRINT-THIS-PRIVATE-KEY"
    monkeypatch.setenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        '{"type": "service_account", "private_key": "made-up"}',
    )
    monkeypatch.setattr(
        drive.service_account.Credentials,
        "from_service_account_info",
        Mock(side_effect=ValueError(secret)),
    )

    with pytest.raises(drive.DriveError) as error:
        drive._load_credentials()

    assert str(error.value) == "Google Drive credentials could not be loaded."
    assert secret not in str(error.value)


@pytest.mark.parametrize("operation", ["list", "get", "export_media"])
def test_request_failures_are_drive_errors_without_secrets(files_api, operation):
    service, files = files_api
    secret = "DO-NOT-PRINT-THIS-PRIVATE-KEY"
    getattr(files, operation).return_value.execute.side_effect = RuntimeError(secret)
    client = drive.DriveClient(service=service)

    with pytest.raises(drive.DriveError) as error:
        if operation == "list":
            client.search("latch")
        else:
            if operation == "export_media":
                files.get.return_value.execute.return_value = {
                    "mimeType": drive.GOOGLE_DOC_MIME_TYPE
                }
            client.read_text("made-up-file-id")

    assert secret not in str(error.value)
