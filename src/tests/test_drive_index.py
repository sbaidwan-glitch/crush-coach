"""The Drive index: chunking, text extraction, full and incremental sync, search.

All files, text and Google responses here are made up (see fake_google.py).
"""

from datetime import datetime

import pytest
from fake_google import (
    DOC,
    FOLDER,
    PDF,
    SHEET,
    doc_paragraph,
    doc_tab,
    http_error,
)
from sqlmodel import select

import drive_index as di
from models import DriveChunk, DriveFile, SyncState


def utc_naive(*parts):
    """A naive UTC datetime, how the database stores times."""
    return datetime(*parts)  # noqa: DTZ001


def files_by_id(session):
    return {f.drive_id: f for f in session.exec(select(DriveFile)).all()}


def chunks_of(session, drive_id):
    file = files_by_id(session)[drive_id]
    return session.exec(
        select(DriveChunk)
        .where(DriveChunk.file_id == file.id)
        .order_by(DriveChunk.position)
    ).all()


# --- chunking ---


def words(n, word="alpha"):
    return " ".join([word] * n)


def test_chunker_splits_at_headings_when_sections_are_big():
    chunks = di.chunk_sections(
        [di.Section("Scope", words(350)), di.Section("Method", words(320, "beta"))]
    )
    assert [c.heading for c in chunks] == ["Scope", "Method"]


def test_chunker_merges_small_sections_and_keeps_their_headings_in_the_text():
    chunks = di.chunk_sections(
        [
            di.Section("A", words(100)),
            di.Section("B", words(100)),
            di.Section("C", words(150)),
        ]
    )
    assert len(chunks) == 1
    assert chunks[0].heading == "A"
    assert "\nB\n" in chunks[0].text or "B\n" in chunks[0].text


def test_chunker_splits_a_huge_section_into_pieces_under_the_limit():
    text = "\n".join(words(40, f"w{i}") for i in range(60))  # 2400 words, 60 lines
    chunks = di.chunk_sections([di.Section("Long", text)])
    assert len(chunks) >= 4
    for chunk in chunks:
        assert di._word_count(chunk.text) <= di.CHUNK_MAX_WORDS
        assert chunk.heading == "Long"


def test_chunker_splits_one_enormous_line_and_skips_empty_sections():
    chunks = di.chunk_sections(
        [di.Section("Blank", "   "), di.Section("PDF", words(1500))]
    )
    assert len(chunks) >= 3
    assert all(di._word_count(c.text) <= di.CHUNK_MAX_WORDS for c in chunks)
    assert all(c.heading == "PDF" for c in chunks)


# --- text extraction helpers ---


def test_doc_sections_read_every_tab_and_prefix_headings_with_the_tab():
    document = {
        "tabs": [
            doc_tab(
                "Overview", doc_paragraph("Intro", "HEADING_1"), doc_paragraph("hello")
            ),
            {
                **doc_tab("Second", doc_paragraph("plain text before any heading")),
                "childTabs": [doc_tab("Nested", doc_paragraph("deep text"))],
            },
        ]
    }
    sections = di.doc_sections(document)
    assert [(s.heading, s.text) for s in sections] == [
        ("Overview › Intro", "hello"),
        ("Second", "plain text before any heading"),
        ("Nested", "deep text"),
    ]


def test_doc_sections_single_tab_uses_plain_headings_and_reads_tables():
    table = {
        "table": {
            "tableRows": [
                {
                    "tableCells": [
                        {"content": [doc_paragraph("Part")]},
                        {"content": [doc_paragraph("Torque")]},
                    ]
                }
            ]
        }
    }
    document = {
        "tabs": [doc_tab("Tab 1", doc_paragraph("Fasteners", "HEADING_2"), table)]
    }
    assert [(s.heading, s.text) for s in di.doc_sections(document)] == [
        ("Fasteners", "Part | Torque")
    ]


def test_sheet_lines_use_header_value_pairs():
    lines = di.sheet_lines(
        [
            [],
            ["Part", "Qty", ""],
            ["M3 screw", "40", "spare"],
            [],
            ["", "", ""],
            ["Nut", 12],
        ]
    )
    assert lines == ["Part: M3 screw; Qty: 40; C: spare", "Part: Nut; Qty: 12"]
    assert di.sheet_lines([]) == []
    assert di.sheet_lines([["Only", "header"]]) == ["Only; header"]


def test_clean_text_removes_nul_and_lone_surrogates():
    assert di.clean_text("a\x00b\ud800c\r\nd") == "abc\nd"


def test_classify():
    assert di.classify("Lid.SLDPRT", "application/octet-stream") == "cad"
    assert di.classify("part.stl", "model/stl") == "cad"
    assert di.classify("CAD", FOLDER) == "folder"
    assert di.classify("x", DOC) == "doc"
    assert di.classify("x", SHEET) == "sheet"
    assert di.classify("x.pdf", PDF) == "pdf"
    assert di.classify("x.zip", "application/zip") == "other"


def test_folder_path_for_is_cycle_safe():
    folders = {"a": ("A", "b"), "b": ("B", "a")}
    assert di.folder_path_for("a", folders) in {"B / A", "A / B"}
    assert di.folder_path_for(None, folders) == "/"
    assert di.folder_path_for("unknown", folders) == "/"


# --- full sync ---


def test_full_sync_indexes_everything(session, fake):
    report = di.full_sync(session, fake.google())

    assert report.mode == "full"
    assert report.text_errors == 0
    files = files_by_id(session)

    lid = files["docLid"]
    assert lid.kind == "doc" and lid.text_status == "ok"
    assert lid.folder_path == "MuddSub / Mechanical / Docs"
    assert lid.modified_time == utc_naive(2026, 10, 5, 9, 0)
    assert lid.web_link == "https://drive.google.com/file/d/docLid/view"
    headings = [c.heading for c in chunks_of(session, "docLid")]
    assert (
        headings == ["Overview › Lid latch notes"] or "Testing › Leak test" in headings
    )
    all_text = " ".join(c.text for c in chunks_of(session, "docLid"))
    assert "Pressurize" in all_text and "holds the enclosure lid" in all_text

    sheet_text = " ".join(c.text for c in chunks_of(session, "sheetParts"))
    assert (
        "Part: Lid latch; Qty: 2" in sheet_text
        and "Part: O-ring 042; Qty: 6" in sheet_text
    )

    pdf = files["pdfData"]
    assert pdf.text_status == "ok" and pdf.size == 2048
    assert "40 N preload" in " ".join(c.text for c in chunks_of(session, "pdfData"))

    big = files["pdfBig"]
    assert big.text_status == "skipped" and "30 MB" in big.text_reason
    assert "20 MB" in big.text_reason
    assert ("drive", "get_media", "pdfBig") not in fake.calls  # never downloaded

    cad = files["cadNew"]
    assert cad.kind == "cad" and cad.text_status == "skipped"
    assert cad.folder_path == "MuddSub / Mechanical / CAD"
    assert chunks_of(session, "cadNew") == []
    assert files["stl"].kind == "cad"

    assert files["word"].text_status == "skipped"
    assert files["fCad"].kind == "folder"
    assert files["fCad"].folder_path == "MuddSub / Mechanical"
    assert files["drv1"].name == "MuddSub" and files["drv1"].folder_path == "/"

    state = session.get(SyncState, 1)
    assert state.page_token == fake.token
    assert state.last_full_sync is not None and state.last_incremental_sync is None
    assert state.file_count == len(files)
    assert state.chunk_count == len(session.exec(select(DriveChunk)).all())
    assert state.error_count == 0
    assert state.running_since is None and state.last_error is None


def test_full_sync_reads_the_docs_api_with_every_tab(session, fake):
    di.full_sync(session, fake.google())  # the fake asserts includeTabsContent=True
    assert ("docs", "get", "docLid") in fake.calls


def test_one_bad_file_is_recorded_and_the_sync_keeps_going(session, fake):
    fake.broken["docLid"] = http_error(403, "The caller does not have permission")
    fake.media["pdfData"] = b"this is not a pdf"

    report = di.full_sync(session, fake.google())

    files = files_by_id(session)
    assert files["docLid"].text_status == "error"
    assert "403" in files["docLid"].text_reason
    assert files["pdfData"].text_status == "error"
    assert files["sheetParts"].text_status == "ok"  # the others were still read
    assert report.text_errors == 2 and len(report.errors) == 2
    assert session.get(SyncState, 1).error_count == 2
    assert session.get(SyncState, 1).last_error is None  # the sync itself worked


def test_second_full_sync_rereads_only_changed_and_failed_files(session, fake):
    fake.broken["docLid"] = http_error(500, "backend error")
    di.full_sync(session, fake.google())
    fake.calls.clear()
    del fake.broken["docLid"]
    fake.files["sheetParts"]["modifiedTime"] = "2026-10-10T00:00:00.000Z"

    report = di.full_sync(session, fake.google())

    read_ids = {call[2] for call in fake.calls}
    assert read_ids == {"docLid", "sheetParts"}  # the failed one and the changed one
    assert report.files_read == 2
    assert files_by_id(session)["docLid"].text_status == "ok"


def test_force_rereads_everything(session, fake):
    di.full_sync(session, fake.google())
    fake.calls.clear()
    di.full_sync(session, fake.google(), force=True)
    assert {c[2] for c in fake.calls} >= {"docLid", "sheetParts", "pdfData"}


def test_full_sync_removes_files_that_left_the_drive(session, fake):
    di.full_sync(session, fake.google())
    del fake.files["docLid"]

    report = di.full_sync(session, fake.google())

    assert "docLid" not in files_by_id(session)
    assert report.removed == 1
    assert chunks_of_count(session) == len(session.exec(select(DriveChunk)).all())
    orphans = [
        c
        for c in session.exec(select(DriveChunk)).all()
        if session.get(DriveFile, c.file_id) is None
    ]
    assert orphans == []


def chunks_of_count(session):
    return session.exec(select(DriveChunk)).all().__len__()


def test_empty_listing_does_not_wipe_the_index(session, fake):
    di.full_sync(session, fake.google())
    count = len(files_by_id(session))
    fake.files.clear()

    with pytest.raises(di.SyncError):
        di.full_sync(session, fake.google())

    assert len(files_by_id(session)) == count
    state = session.get(SyncState, 1)
    assert state.running_since is None
    assert "kept" in state.last_error


def test_listing_failure_is_reported_and_releases_the_lock(session, fake):
    fake.fail_listing = http_error(503, "backend unavailable")

    with pytest.raises(di.SyncError, match="503"):
        di.full_sync(session, fake.google())

    state = session.get(SyncState, 1)
    assert state.running_since is None
    assert "503" in state.last_error
    assert state.page_token is None


def test_missing_credentials_are_a_clear_sync_error(session, monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with pytest.raises(di.SyncError, match="credentials are not configured"):
        di.full_sync(session)

    assert "credentials" in session.get(SyncState, 1).last_error


def test_text_over_the_cap_is_cut_short_and_says_so(session, fake, monkeypatch):
    monkeypatch.setattr(di, "MAX_TEXT_CHARS", 300)
    fake.documents["docLid"] = {
        "tabs": [doc_tab("T", doc_paragraph(" ".join(["wordy"] * 200)))]
    }
    di.full_sync(session, fake.google())
    lid = files_by_id(session)["docLid"]
    assert lid.text_status == "ok" and "cut short" in lid.text_reason
    assert sum(len(c.text) for c in chunks_of(session, "docLid")) <= 300


# --- the lock ---


def test_a_second_sync_is_refused_while_one_runs(session, fake):
    di.claim(session)
    with pytest.raises(di.SyncAlreadyRunning):
        di.full_sync(session, fake.google())
    assert session.get(SyncState, 1).running_since is not None  # still held


def test_a_stale_lock_is_taken_over(session, fake):
    state = di.get_state(session)
    state.running_since = di.utc_now() - di.STALE_LOCK_AFTER - di.timedelta(minutes=1)
    session.add(state)
    session.commit()
    di.full_sync(session, fake.google())
    assert session.get(SyncState, 1).running_since is None


def test_already_claimed_skips_the_claim_and_releases(session, fake):
    di.claim(session)
    di.incremental_sync(session, fake.google(), already_claimed=True)
    assert session.get(SyncState, 1).running_since is None


# --- incremental sync ---


def synced(session, fake):
    di.full_sync(session, fake.google())
    fake.calls.clear()


def test_incremental_without_a_token_does_a_full_sync(session, fake):
    report = di.incremental_sync(session, fake.google())
    assert report.mode == "full"
    assert session.get(SyncState, 1).page_token == fake.token


def test_incremental_with_nothing_new_reads_nothing(session, fake):
    synced(session, fake)
    report = di.incremental_sync(session, fake.google())
    assert report.mode == "incremental" and report.files_read == 0
    assert fake.calls == []
    assert session.get(SyncState, 1).last_incremental_sync is not None


def test_incremental_rereads_a_changed_doc(session, fake):
    synced(session, fake)
    fake.documents["docLid"] = {
        "tabs": [
            doc_tab("Tab 1", doc_paragraph("Rewritten with a new gasket section."))
        ]
    }
    fake.files["docLid"]["modifiedTime"] = "2026-11-01T08:00:00.000Z"
    fake.record_change("docLid")

    report = di.incremental_sync(session, fake.google())

    assert report.files_read == 1 and {c[2] for c in fake.calls} == {"docLid"}
    assert [c.text for c in chunks_of(session, "docLid")] == [
        "Rewritten with a new gasket section."
    ]
    assert files_by_id(session)["docLid"].modified_time == utc_naive(2026, 11, 1, 8, 0)


def test_incremental_adds_new_files_in_new_folders(session, fake):
    synced(session, fake)
    fake.add("fNew", "Thrusters", FOLDER, "fMech")
    fake.add("cadT", "Thruster_mount.STEP", "application/octet-stream", "fNew")
    fake.record_change("fNew")
    fake.record_change("cadT")

    di.incremental_sync(session, fake.google())

    new = files_by_id(session)["cadT"]
    assert new.kind == "cad" and new.folder_path == "MuddSub / Mechanical / Thrusters"


def test_incremental_removes_trashed_and_removed_files(session, fake):
    synced(session, fake)
    fake.files["docLid"]["trashed"] = True
    fake.record_change("docLid")
    del fake.files["sheetParts"]
    fake.record_change("sheetParts", removed=True)

    report = di.incremental_sync(session, fake.google())

    files = files_by_id(session)
    assert "docLid" not in files and "sheetParts" not in files
    assert report.removed == 2
    assert chunks_of_count(session) == session.get(SyncState, 1).chunk_count


def test_trashing_a_folder_removes_what_was_inside(session, fake):
    synced(session, fake)
    fake.files["fCad"]["trashed"] = True
    fake.record_change("fCad")

    di.incremental_sync(session, fake.google())

    files = files_by_id(session)
    assert {"fCad", "cadOld", "cadNew", "stl"}.isdisjoint(files)
    assert "docLid" in files


def test_renaming_a_folder_updates_the_paths_of_its_files(session, fake):
    synced(session, fake)
    fake.files["fMech"]["name"] = "Mechanical 2026"
    fake.record_change("fMech")

    di.incremental_sync(session, fake.google())

    files = files_by_id(session)
    assert files["cadNew"].folder_path == "MuddSub / Mechanical 2026 / CAD"
    assert files["docLid"].folder_path == "MuddSub / Mechanical 2026 / Docs"
    assert fake.calls == []  # no text was re-read for a rename


def test_incremental_survives_a_failing_file_and_still_advances(session, fake):
    synced(session, fake)
    fake.files["pdfData"]["modifiedTime"] = "2026-11-02T00:00:00.000Z"
    fake.broken["pdfData"] = http_error(500, "boom")
    fake.record_change("pdfData")

    report = di.incremental_sync(session, fake.google())

    assert report.text_errors == 1
    assert files_by_id(session)["pdfData"].text_status == "error"
    assert session.get(SyncState, 1).page_token == fake.token


def test_a_rejected_token_falls_back_to_a_full_sync(session, fake):
    synced(session, fake)
    fake.invalid_tokens.add(session.get(SyncState, 1).page_token)

    report = di.incremental_sync(session, fake.google())

    assert report.mode == "full"
    assert session.get(SyncState, 1).last_error is None


def test_a_changes_feed_failure_keeps_the_old_token(session, fake, monkeypatch):
    synced(session, fake)
    old = session.get(SyncState, 1).page_token
    fake.record_change("docLid")

    def boom(services, token):
        raise http_error(500, "backend error")

    monkeypatch.setattr(di, "_list_changes", boom)
    with pytest.raises(di.SyncError, match="500"):
        di.incremental_sync(session, fake.google())

    state = session.get(SyncState, 1)
    assert state.page_token == old and state.running_since is None


# --- credentials and scopes ---


def test_services_ask_for_read_only_scopes_only(monkeypatch):
    seen = {}

    def fake_load(scopes=None):
        seen["scopes"] = scopes
        return object()

    monkeypatch.setattr(di.drive, "_load_credentials", fake_load)
    monkeypatch.setattr(di, "build", lambda *a, **k: object())

    di.GoogleServices.from_environment()

    assert seen["scopes"], "scopes must be passed explicitly"
    assert all(scope.endswith(".readonly") for scope in seen["scopes"])


# --- search (SQLite: LIKE fallback) ---


def test_search_finds_chunks_with_file_details(session, fake):
    di.full_sync(session, fake.google())

    hits = di.search(session, "lid latch")

    assert hits, "expected results"
    top = hits[0]
    assert top.name in {"Lid latch design notes", "Latch datasheet.pdf", "Parts list"}
    assert top.folder_path == "MuddSub / Mechanical / Docs"
    assert top.web_link.startswith("https://drive.google.com/")
    assert top.modified_time is not None
    assert "latch" in top.snippet.lower()
    # A file whose name has every query word outranks a content-only match.
    assert hits[0].name == "Lid latch design notes"


def test_search_finds_cad_by_name_folder_and_kind_and_prefers_the_newest(session, fake):
    di.full_sync(session, fake.google())

    hits = di.search(session, "enclosure CAD")

    names = [h.name for h in hits]
    assert names[:2] == ["Enclosure_v2.SLDPRT", "Enclosure_v1.SLDPRT"]  # newest first
    assert all(h.text == "" and h.chunk_id is None for h in hits[:2])
    assert hits[0].folder_path == "MuddSub / Mechanical / CAD"
    assert hits[0].modified_time == utc_naive(2026, 10, 9, 10, 0)


def test_search_matches_part_numbers_split_by_underscores(session, fake):
    di.full_sync(session, fake.google())
    assert [h.name for h in di.search(session, "mount")][:1] == ["lid_mount.stl"]


def test_search_loosens_to_any_word_when_nothing_has_them_all(session, fake):
    di.full_sync(session, fake.google())
    hits = di.search(session, "gasket leak purple")
    assert any(h.name == "Lid latch design notes" for h in hits)


def test_search_caps_chunks_per_file_and_limit(session, fake):
    fake.documents["docLid"] = {
        "tabs": [
            doc_tab(
                "T", *[doc_paragraph(f"Section {i}", "HEADING_1") for i in range(1)]
            )
        ]
    }
    sections = [di.Section(f"Part {i}", words(320, "latch")) for i in range(5)]
    document_paragraphs = []
    for s in sections:
        document_paragraphs += [
            doc_paragraph(s.heading, "HEADING_1"),
            doc_paragraph(s.text),
        ]
    fake.documents["docLid"] = {"tabs": [doc_tab("T", *document_paragraphs)]}
    di.full_sync(session, fake.google())

    capped = [
        h for h in di.search(session, "latch", limit=20) if h.drive_id == "docLid"
    ]
    assert len(capped) == 3
    uncapped = di.search(session, "latch", limit=20, max_per_file=None)
    assert len([h for h in uncapped if h.drive_id == "docLid"]) == 5
    assert len(di.search(session, "latch", limit=2)) == 2


def test_search_ties_go_to_the_newer_file(session, fake):
    fake.add("twinOld", "Twin plan", DOC, "fDocs", modified="2026-01-01T00:00:00Z")
    fake.add("twinNew", "Twin plan", DOC, "fDocs", modified="2026-06-01T00:00:00Z")
    for twin in ("twinOld", "twinNew"):
        fake.documents[twin] = {
            "tabs": [doc_tab("T", doc_paragraph("gasket torque plan"))]
        }
    di.full_sync(session, fake.google())

    hits = di.search(session, "gasket torque")
    assert [h.drive_id for h in hits[:2]] == ["twinNew", "twinOld"]


def test_search_edge_cases(session, fake):
    di.full_sync(session, fake.google())
    assert di.search(session, "") == []
    assert di.search(session, "   ") == []
    assert di.search(session, "latch", limit=0) == []
    assert di.search(session, "%") == [] and di.search(session, "_ _") == []
    assert di.search(session, "100%_;DROP TABLE drive_file") == []  # just words
    assert len(session.exec(select(DriveFile)).all()) > 0
    assert di.search(session, "nothing matches this xyzzy") == []


def test_make_snippet_centers_on_the_match():
    body = "start " + "filler " * 80 + "the latch torque is here " + "tail " * 80
    snippet = di.make_snippet(body, ["latch"])
    assert (
        "latch torque" in snippet and snippet.startswith("…") and snippet.endswith("…")
    )
    assert len(snippet) <= di.SNIPPET_CHARS + 2


# --- the command-line scripts ---


def test_sync_script_runs_a_full_sync_and_reports(session, fake, monkeypatch, capsys):
    from scripts import sync_drive

    monkeypatch.setattr(di.GoogleServices, "from_environment", lambda: fake.google())
    fake.broken["docLid"] = http_error(403, "no access")

    assert sync_drive.main(["--full"]) == 0

    out = capsys.readouterr().out
    assert "full sync done" in out and "1 with unreadable text" in out
    assert "Lid latch design notes: Google API error 403" in out
    assert sync_drive.main(["--incremental"]) == 0
    assert "incremental sync done" in capsys.readouterr().out


def test_sync_script_exit_codes(session, monkeypatch, capsys):
    from scripts import sync_drive

    def no_credentials():
        raise di.DriveError("Google Drive credentials are not configured.")

    monkeypatch.setattr(di.GoogleServices, "from_environment", no_credentials)
    assert sync_drive.main(["--incremental"]) == 1
    assert "credentials are not configured" in capsys.readouterr().err

    di.claim(session)  # someone else is mid-sync: skip quietly, exit 0
    assert sync_drive.main(["--incremental"]) == 0
    assert "already running" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        sync_drive.main([])  # one of --full / --incremental is required
    with pytest.raises(SystemExit):
        sync_drive.main(["--incremental", "--force"])


def test_index_search_script_prints_results(session, fake, monkeypatch, capsys):
    from scripts import index_search

    monkeypatch.setattr("sys.argv", ["index_search.py", "lid latch"])
    assert index_search.main() == 0
    assert "index is empty" in capsys.readouterr().out

    di.full_sync(session, fake.google())
    assert index_search.main() == 0
    out = capsys.readouterr().out
    assert "1. Lid latch design notes" in out
    assert "MuddSub / Mechanical / Docs" in out
    assert "https://drive.google.com/file/d/docLid/view" in out

    monkeypatch.setattr("sys.argv", ["index_search.py", "zzzz nothing"])
    assert index_search.main() == 0
    assert "No matches" in capsys.readouterr().out


# --- the lock is always released ---


def test_the_lock_is_released_when_the_process_is_interrupted(
    session, fake, monkeypatch
):
    """Ctrl-C and SIGTERM are not `Exception`s; the lock must still be freed."""

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(di, "_run_full", interrupted)

    with pytest.raises(KeyboardInterrupt):
        di.full_sync(session, fake.google())

    state = session.get(SyncState, 1)
    assert state.running_since is None
    assert state.last_error == di.INTERRUPTED
    monkeypatch.undo()  # and the next sync is not blocked:
    assert di.full_sync(session, fake.google()).mode == "full"
    assert session.get(SyncState, 1).last_error is None


def test_a_failed_release_does_not_hide_the_real_error(session, fake, monkeypatch):
    fake.fail_listing = http_error(503, "backend unavailable")

    def cannot_release(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(di, "release", cannot_release)

    with pytest.raises(di.SyncError, match="503"):  # not RuntimeError
        di.full_sync(session, fake.google())


def test_the_lock_is_released_after_success_and_after_any_failure(
    session, fake, monkeypatch
):
    di.full_sync(session, fake.google())
    assert session.get(SyncState, 1).running_since is None

    def broken_feed(services, token):
        raise http_error(500, "boom")

    monkeypatch.setattr(di, "_list_changes", broken_feed)
    with pytest.raises(di.SyncError):
        di.incremental_sync(session, fake.google())
    assert session.get(SyncState, 1).running_since is None


def test_a_lock_just_under_two_hours_old_still_blocks(session, fake):
    state = di.get_state(session)
    state.running_since = di.utc_now() - di.STALE_LOCK_AFTER + di.timedelta(minutes=1)
    session.add(state)
    session.commit()
    with pytest.raises(di.SyncAlreadyRunning):
        di.full_sync(session, fake.google())


# --- stop signals in the script ---


def test_sigterm_becomes_a_normal_exit_so_the_lock_is_released():
    import signal

    from scripts import sync_drive

    previous = signal.getsignal(signal.SIGTERM)
    try:
        sync_drive.install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit) as stop:
            handler(signal.SIGTERM, None)
        assert stop.value.code == 128 + signal.SIGTERM
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_sighup_is_left_alone_under_nohup(monkeypatch):
    import signal

    from scripts import sync_drive

    installed = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: installed.append(sig))
    real_get = signal.getsignal
    monkeypatch.setattr(
        signal,
        "getsignal",
        lambda sig: signal.SIG_IGN if sig == signal.SIGHUP else real_get(sig),
    )

    sync_drive.install_signal_handlers()

    assert installed == [signal.SIGTERM]  # SIGHUP stays ignored, as nohup set it
