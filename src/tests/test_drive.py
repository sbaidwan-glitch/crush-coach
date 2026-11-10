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
            "folder_path": "/",
            "locations": [
                {
                    "link": "https://example.test/latch",
                    "mime_type": "application/vnd.google-apps.document",
                    "modified_time": "2026-11-06T20:15:00.000Z",
                    "folder_path": "/",
                }
            ],
        }
    ]
    files.list.assert_called_once_with(
        q=("trashed = false and (name contains 'latch' or fullText contains 'latch')"),
        pageSize=1000,
        fields=(
            "nextPageToken,files(id,name,webViewLink,mimeType,modifiedTime,parents)"
        ),
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


def test_search_ranks_file_name_matches_above_newer_full_text_matches(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {
        "files": [
            {
                "id": "full-text-only",
                "name": "New seal notes",
                "webViewLink": "https://example.test/full-text",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-11-07T00:00:00.000Z",
            },
            {
                "id": "name-match",
                "name": "Latch design",
                "webViewLink": "https://example.test/name",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2025-01-01T00:00:00.000Z",
            },
        ]
    }

    results = drive.DriveClient(service=service).search("latch")

    assert [result["title"] for result in results] == [
        "Latch design",
        "New seal notes",
    ]


def test_search_groups_duplicate_names_and_includes_year_in_paths(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {
        "files": [
            {
                "id": "new-latch",
                "name": "Latch design",
                "webViewLink": "https://example.test/new",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-11-01T00:00:00.000Z",
                "parents": ["new-mechanical"],
            },
            {
                "id": "old-latch",
                "name": "Latch design",
                "webViewLink": "https://example.test/old",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2025-11-01T00:00:00.000Z",
                "parents": ["old-mechanical"],
            },
        ]
    }
    folders = {
        "new-mechanical": {
            "id": "new-mechanical",
            "name": "Mechanical",
            "parents": ["year-2026"],
        },
        "year-2026": {"id": "year-2026", "name": "2026", "parents": ["root"]},
        "old-mechanical": {
            "id": "old-mechanical",
            "name": "Mechanical",
            "parents": ["year-2025"],
        },
        "year-2025": {"id": "year-2025", "name": "2025", "parents": ["root"]},
        "root": {"id": "root", "name": "MuddSub", "parents": []},
    }

    def get_folder(**kwargs):
        request = Mock()
        request.execute.return_value = folders[kwargs["fileId"]]
        return request

    files.get.side_effect = get_folder

    results = drive.DriveClient(service=service).search("latch")

    assert len(results) == 1
    assert results[0]["title"] == "Latch design"
    assert results[0]["folder_path"] == "MuddSub / 2026 / Mechanical"
    assert [location["folder_path"] for location in results[0]["locations"]] == [
        "MuddSub / 2026 / Mechanical",
        "MuddSub / 2025 / Mechanical",
    ]
    root_calls = [
        call for call in files.get.call_args_list if call.kwargs["fileId"] == "root"
    ]
    assert len(root_calls) == 1


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


def test_search_allows_more_than_ten_results(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {
        "files": [
            {
                "id": f"file-{number}",
                "name": f"Latch notes {number:02d}",
                "mimeType": "text/plain",
                "modifiedTime": f"2026-10-{number:02d}T00:00:00.000Z",
            }
            for number in range(1, 16)
        ]
    }

    results = drive.DriveClient(service=service).search("latch", limit=12)

    assert len(results) == 12


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


# --- file-type filter ---

CAD_FILES = [
    {
        "id": "cad-1",
        "name": "MdSb26_Crush_Enclosure.SLDASM",
        "webViewLink": "https://example.test/asm",
        "mimeType": "application/octet-stream",
        "modifiedTime": "2026-10-30T10:00:00.000Z",
    },
    {
        "id": "cad-2",
        "name": "MdSb26_Crush_Baseplate.sldprt",
        "webViewLink": "https://example.test/prt",
        "mimeType": "application/octet-stream",
        "modifiedTime": "2026-10-01T10:00:00.000Z",
    },
    {
        "id": "cad-3",
        "name": "Enclosure.STP",
        "webViewLink": "https://example.test/stp",
        "mimeType": "application/octet-stream",
        "modifiedTime": "2026-09-01T10:00:00.000Z",
    },
    {  # matched Drive's word search but is not CAD
        "id": "not-cad",
        "name": "SLDASM naming guide.txt",
        "webViewLink": "https://example.test/guide",
        "mimeType": "text/plain",
        "modifiedTime": "2026-11-01T10:00:00.000Z",
    },
]


def test_cad_search_matches_extensions_and_names_and_skips_full_text(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {"files": CAD_FILES}

    results = drive.DriveClient(service=service).search("enclosure", file_type="cad")

    query = files.list.call_args.kwargs["q"]
    for extension in ("SLDASM", "SLDPRT", "SLDDRW", "STEP", "STP"):
        assert f"name contains '{extension}'" in query
    assert "name contains 'enclosure'" in query
    assert "fullText" not in query
    assert query.startswith("trashed = false and ")
    # Exact-suffix filter, case-insensitive; name matches first, then newest.
    assert [r["title"] for r in results] == [
        "MdSb26_Crush_Enclosure.SLDASM",
        "Enclosure.STP",
        "MdSb26_Crush_Baseplate.sldprt",
    ]
    assert "SLDASM naming guide.txt" not in {r["title"] for r in results}
    assert results[0]["title"] == "MdSb26_Crush_Enclosure.SLDASM"


def test_cad_search_without_a_query_lists_the_newest_cad_files(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {"files": CAD_FILES}

    results = drive.DriveClient(service=service).search("", file_type="cad")

    assert "name contains ''" not in files.list.call_args.kwargs["q"]
    assert results[0]["title"] == "MdSb26_Crush_Enclosure.SLDASM"
    assert len(results) == 3


def test_multi_word_cad_queries_match_each_word(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {"files": []}

    drive.DriveClient(service=service).search("crush enclosure", file_type="cad")

    query = files.list.call_args.kwargs["q"]
    assert "name contains 'crush'" in query and "name contains 'enclosure'" in query


@pytest.mark.parametrize(
    ("file_type", "mime_types"),
    [
        (
            "sheets",
            [
                "application/vnd.google-apps.spreadsheet",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/csv",
            ],
        ),
        (
            "docs",
            [
                "application/vnd.google-apps.document",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ],
        ),
        ("pdf", ["application/pdf"]),
    ],
)
def test_other_file_types_filter_by_mime_type_and_keep_text_search(
    files_api, file_type, mime_types
):
    service, files = files_api
    files.list.return_value.execute.return_value = {"files": []}

    drive.DriveClient(service=service).search("budget", file_type=file_type)

    query = files.list.call_args.kwargs["q"]
    assert "(name contains 'budget' or fullText contains 'budget')" in query
    for mime_type in mime_types:
        assert f"mimeType = '{mime_type}'" in query
    kwargs = files.list.call_args.kwargs
    assert kwargs["supportsAllDrives"] and kwargs["includeItemsFromAllDrives"]


def test_unknown_file_type_is_a_clear_error(files_api):
    service, files = files_api

    with pytest.raises(drive.DriveError, match="cad, sheets, docs, pdf"):
        drive.DriveClient(service=service).search("x", file_type="video")

    files.list.assert_not_called()


def test_typed_searches_stop_after_a_few_pages(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {
        "files": [],
        "nextPageToken": "more",
    }

    drive.DriveClient(service=service).search("", file_type="cad")

    assert files.list.call_count == drive.MAX_TYPED_SEARCH_PAGES


def test_search_without_a_type_is_unchanged(files_api):
    service, files = files_api
    files.list.return_value.execute.return_value = {"files": []}

    drive.DriveClient(service=service).search("o-ring")

    assert files.list.call_args.kwargs["q"] == (
        "trashed = false and (name contains 'o-ring' or fullText contains 'o-ring')"
    )


# --- list_folder ---

FOLDER = drive.GOOGLE_FOLDER_MIME_TYPE
CAD_ID = "folderCAD1234567890abcdef"
OLD_CAD_ID = "folderOLDCAD1234567890ab"


def folder_service(files_api, *, folders_by_name=None, children=None, info=None):
    """A fake Drive: name lookups, parent lookups, one folder's children."""
    service, files = files_api
    tree = {
        "driveroot0000000000000001": {
            "id": "driveroot0000000000000001",
            "name": "MuddSub",
            "mimeType": FOLDER,
        },
        "mech00000000000000000001": {
            "id": "mech00000000000000000001",
            "name": "Mechanical",
            "mimeType": FOLDER,
            "parents": ["driveroot0000000000000001"],
        },
        "mech2400000000000000000": {
            "id": "mech2400000000000000000",
            "name": "Mechanical",
            "mimeType": FOLDER,
            "parents": ["driveroot0000000000000001"],
        },
        CAD_ID: {
            "id": CAD_ID,
            "name": "CAD",
            "mimeType": FOLDER,
            "parents": ["mech00000000000000000001"],
            "modifiedTime": "2026-10-20T00:00:00.000Z",
        },
        **(info or {}),
    }

    def get(fileId, **kwargs):
        call = Mock()
        call.execute.return_value = tree[fileId]
        return call

    def list_(q, **kwargs):
        call = Mock()
        if "mimeType = '" + FOLDER + "'" in q:
            call.execute.return_value = {"files": folders_by_name or []}
        else:
            call.execute.return_value = {"files": children or []}
        return call

    files.get.side_effect = get
    files.list.side_effect = list_
    return service, files


CHILDREN = [
    {
        "id": "f1",
        "name": "Finalized",
        "mimeType": FOLDER,
        "modifiedTime": "2026-09-01T00:00:00.000Z",
        "webViewLink": "https://example.test/finalized",
    },
    {
        "id": "f2",
        "name": "MdSb26_Crush_Enclosure.SLDASM",
        "mimeType": "application/octet-stream",
        "modifiedTime": "2026-10-30T00:00:00.000Z",
        "webViewLink": "https://example.test/asm",
    },
    {
        "id": "f3",
        "name": "Seal calculations",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "modifiedTime": "2026-10-01T00:00:00.000Z",
    },
    {
        "id": "f4",
        "name": "Archive",
        "mimeType": FOLDER,
        "modifiedTime": "2026-11-01T00:00:00.000Z",
        "webViewLink": "https://example.test/archive",
    },
]


def test_list_folder_by_id_lists_folders_first_then_newest(files_api):
    service, files = folder_service(files_api, children=CHILDREN)

    listing = drive.DriveClient(service=service).list_folder(CAD_ID)

    assert listing["folder_path"] == "MuddSub / Mechanical / CAD"
    assert listing["folder_id"] == CAD_ID
    assert [(e["name"], e["kind"]) for e in listing["entries"]] == [
        ("Archive", "folder"),
        ("Finalized", "folder"),
        ("MdSb26_Crush_Enclosure.SLDASM", "cad"),
        ("Seal calculations", "sheet"),
    ]
    assert listing["entries"][2]["modified_time"] == "2026-10-30T00:00:00.000Z"
    assert listing["entries"][3]["link"] == "https://drive.google.com/open?id=f3"
    assert listing["total"] == 4
    query = files.list.call_args.kwargs["q"]
    assert f"'{CAD_ID}' in parents" in query and "trashed = false" in query
    assert files.list.call_args.kwargs["supportsAllDrives"] is True
    assert files.list.call_args.kwargs["includeItemsFromAllDrives"] is True


def test_list_folder_accepts_a_folder_link(files_api):
    service, _files = folder_service(files_api, children=CHILDREN)

    listing = drive.DriveClient(service=service).list_folder(
        f"https://drive.google.com/drive/folders/{CAD_ID}?usp=sharing"
    )

    assert listing["folder_id"] == CAD_ID


def test_list_folder_by_path_finds_the_one_matching_folder(files_api):
    service, _files = folder_service(
        files_api,
        folders_by_name=[
            {
                "id": CAD_ID,
                "name": "CAD",
                "parents": ["mech00000000000000000001"],
                "modifiedTime": "2026-10-20T00:00:00.000Z",
            },
            {  # a CAD folder somewhere else does not match "Mechanical/CAD"
                "id": "other0000000000000000001",
                "name": "CAD",
                "parents": ["driveroot0000000000000001"],
            },
        ],
        children=CHILDREN,
    )

    listing = drive.DriveClient(service=service).list_folder("Mechanical/CAD")

    assert listing["folder_path"] == "MuddSub / Mechanical / CAD"
    assert len(listing["entries"]) == 4
    assert listing["candidates"] == []


def test_ambiguous_path_returns_candidates_instead_of_guessing(files_api):
    service, _files = folder_service(
        files_api,
        folders_by_name=[
            {
                "id": OLD_CAD_ID,
                "name": "CAD",
                "parents": ["mech2400000000000000000"],
                "modifiedTime": "2025-05-01T00:00:00.000Z",
            },
            {
                "id": CAD_ID,
                "name": "CAD",
                "parents": ["mech00000000000000000001"],
                "modifiedTime": "2026-10-20T00:00:00.000Z",
            },
        ],
        children=CHILDREN,
    )

    listing = drive.DriveClient(service=service).list_folder("CAD")

    assert listing["entries"] == []
    assert [c["id"] for c in listing["candidates"]] == [CAD_ID, OLD_CAD_ID]  # newest
    assert listing["candidates"][0]["folder_path"] == "MuddSub / Mechanical / CAD"


def test_missing_folder_is_not_found_not_a_drive_failure(files_api):
    service, _files = folder_service(files_api, folders_by_name=[])

    with pytest.raises(drive.FolderNotFound, match="Mechanical/Nope"):
        drive.DriveClient(service=service).list_folder("Mechanical/Nope")


def test_an_id_that_is_not_a_folder_is_not_found(files_api):
    service, _files = folder_service(
        files_api,
        info={
            "docid0000000000000000001": {
                "id": "docid0000000000000000001",
                "name": "Notes",
                "mimeType": "application/vnd.google-apps.document",
            }
        },
    )

    with pytest.raises(drive.FolderNotFound):
        drive.DriveClient(service=service).list_folder("docid0000000000000000001")


def test_empty_folder_lists_nothing(files_api):
    service, _files = folder_service(files_api, children=[])

    listing = drive.DriveClient(service=service).list_folder(CAD_ID)

    assert listing["entries"] == [] and listing["total"] == 0


def test_list_folder_limit_reports_the_full_count(files_api):
    service, _files = folder_service(files_api, children=CHILDREN)

    listing = drive.DriveClient(service=service).list_folder(CAD_ID, limit=2)

    assert len(listing["entries"]) == 2 and listing["total"] == 4


@pytest.mark.parametrize("operation", ["list", "get"])
def test_list_folder_failures_are_safe_drive_errors(files_api, operation):
    service, files = files_api
    secret = "DO-NOT-PRINT-THIS-PRIVATE-KEY"
    getattr(files, operation).return_value.execute.side_effect = RuntimeError(secret)
    client = drive.DriveClient(service=service)

    with pytest.raises(drive.DriveError) as error:
        client.list_folder(CAD_ID if operation == "get" else "Mechanical/CAD")

    assert not isinstance(error.value, drive.FolderNotFound)
    assert secret not in str(error.value)
