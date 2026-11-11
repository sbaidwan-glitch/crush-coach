"""The exclude rules (config, not code) and how the sync applies them."""

import logging

import pytest
from fake_google import DOC, FOLDER, doc_paragraph, doc_tab
from sqlmodel import select

import drive_exclude as ex
import drive_index as di
from models import DriveFile, SyncState


def match(rules, folders, name, mime="application/octet-stream", is_folder=False):
    rule = ex.parse_rules(rules).match(folders, name, mime, is_folder)
    return rule.text if rule else None


# --- the rule language ---


def test_a_folder_name_excludes_that_folder_anywhere_and_everything_in_it():
    rules = [".git"]
    assert match(rules, [], ".git", is_folder=True) == ".git"
    assert match(rules, ["Software", ".git", "objects"], "abc") == ".git"
    assert match(rules, ["Software"], "Notes") is None
    assert match(rules, ["Software"], ".gitignore") is None  # not the same name


def test_matching_ignores_case_and_supports_wildcards():
    assert match(["node_modules"], ["App", "Node_Modules"], "x") == "node_modules"
    assert match(["pool_run_images*"], ["pool_run_images1"], "x") == "pool_run_images*"
    assert match(["log?"], ["logs"], "x") == "log?"


def test_a_path_rule_needs_the_folders_in_order_and_side_by_side():
    rules = ["Pool Runs/Raw"]
    assert match(rules, ["2023", "Pool Runs", "Raw"], "clip") == "Pool Runs/Raw"
    assert match(rules, ["Raw", "Pool Runs"], "clip") is None
    assert match(rules, ["Pool Runs", "Notes", "Raw"], "clip") is None
    assert match(rules, [], "Raw", is_folder=True) is None  # only the second half


def test_file_and_mime_rules_match_files_not_folders():
    rules = ["file: *.log", "mime: image/*"]
    assert match(rules, ["A"], "run.LOG") == "file: *.log"
    assert match(rules, ["A"], "front.png", "image/png") == "mime: image/*"
    assert match(rules, ["A"], "notes.txt", "text/plain") is None
    assert match(rules, ["A"], "run.log", is_folder=True) is None  # a folder named so


def test_comments_blanks_and_bad_lines_are_ignored(caplog):
    with caplog.at_level(logging.WARNING, logger="crush_coach"):
        rules = ex.parse_rules(["# a comment", "", "  ", "file:", "/", ".git"])
    assert [r.text for r in rules.rules] == [".git"]
    assert caplog.text.count("ignoring an empty exclude rule") == 2


def test_rules_load_from_the_file_and_the_environment_variable(tmp_path):
    path = tmp_path / "rules.txt"
    path.write_text("# mine\n.git\n")
    rules = ex.load_rules(path, {ex.ENV_VAR: "Photos;file: *.tmp\nBackups"})
    assert [r.text for r in rules.rules] == [".git", "Photos", "file: *.tmp", "Backups"]


def test_a_missing_rules_file_is_a_warning_not_a_failure(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="crush_coach"):
        rules = ex.load_rules(tmp_path / "nope.txt", {ex.ENV_VAR: "Photos"})
    assert [r.text for r in rules.rules] == ["Photos"]
    assert "could not read" in caplog.text


def test_the_committed_default_rules_cover_the_noise_found_in_the_real_drive():
    rules = ex.load_rules(environ={})
    assert match_obj(rules, ["Alfie CAD", ".grabcad", ".filecache"], "9D01.pdf")
    assert match_obj(rules, ["Repo", ".git", "hooks"], "pre-commit.sample")
    assert match_obj(rules, ["Photos"], "IMG_1.jpg", "image/jpeg")
    assert match_obj(rules, ["Dive"], "run.mp4", "video/mp4")
    assert not match_obj(
        rules, ["Mechanical"], "Lid latch notes", "application/vnd.google-apps.document"
    )
    assert not match_obj(rules, ["Mechanical"], "Lid.SLDPRT", "application/sldworks")
    assert not match_obj(rules, ["Mechanical"], "Seals.pdf", "application/pdf")


def match_obj(rules, folders, name, mime="application/octet-stream"):
    return rules.match(folders, name, mime, False)


# --- in a sync ---


@pytest.fixture
def noisy(fake):
    """The fixture drive plus a code repo, a photo folder and a log file."""
    fake.add("fGit", ".git", FOLDER, "fMech")
    fake.add("fHooks", "hooks", FOLDER, "fGit")
    fake.add("gitFile", "pre-commit.sample", "text/plain", "fHooks")
    fake.add("fPhotos", "Pool photos", FOLDER, "fMech")
    fake.add("photo1", "IMG_0001.jpg", "image/jpeg", "fPhotos")
    fake.add("photo2", "latch closeup.png", "image/png", "fDocs")  # a photo anywhere
    fake.add("log1", "run.log", "text/plain", "fDocs")
    return fake


RULES = ".git\nPool photos\nmime: image/*\nfile: *.log"


def test_a_full_sync_leaves_out_excluded_items_entirely(session, noisy):
    report = di.full_sync(
        session, noisy.google(), exclude=ex.parse_rules(RULES.split("\n"))
    )

    ids = {f.drive_id for f in session.exec(select(DriveFile)).all()}
    assert {
        "fGit",
        "fHooks",
        "gitFile",
        "fPhotos",
        "photo1",
        "photo2",
        "log1",
    }.isdisjoint(ids)
    assert {"docLid", "fMech", "fDocs", "cadNew"} <= ids  # the real files are all there
    assert report.excluded == 7
    assert di.search(session, "latch closeup") == [] or all(
        h.drive_id != "photo2" for h in di.search(session, "latch closeup")
    )


def test_adding_a_rule_and_running_full_again_removes_what_is_already_indexed(
    session, noisy
):
    di.full_sync(session, noisy.google(), exclude=ex.parse_rules([]))
    assert "photo2" in {f.drive_id for f in session.exec(select(DriveFile)).all()}

    report = di.full_sync(
        session, noisy.google(), exclude=ex.parse_rules(["mime: image/*"])
    )

    ids = {f.drive_id for f in session.exec(select(DriveFile)).all()}
    assert {"photo1", "photo2"}.isdisjoint(ids)
    assert "log1" in ids
    assert report.removed == 2


def test_no_rules_means_nothing_is_excluded(session, noisy):
    report = di.full_sync(session, noisy.google(), exclude=ex.parse_rules([]))
    assert report.excluded == 0
    assert "gitFile" in {f.drive_id for f in session.exec(select(DriveFile)).all()}


def test_incremental_skips_files_changed_inside_an_excluded_folder(session, noisy):
    rules = ex.parse_rules(RULES.split("\n"))
    di.full_sync(session, noisy.google(), exclude=rules)
    noisy.add("gitNew", "HEAD", "text/plain", "fHooks")  # deep inside the excluded .git
    noisy.record_change("gitNew")
    noisy.add("docNew", "New notes", DOC, "fDocs")
    noisy.documents["docNew"] = {"tabs": [doc_tab("T", doc_paragraph("fresh text"))]}
    noisy.record_change("docNew")
    noisy.calls.clear()

    report = di.incremental_sync(session, noisy.google(), exclude=rules)

    ids = {f.drive_id for f in session.exec(select(DriveFile)).all()}
    assert "gitNew" not in ids and "docNew" in ids
    assert report.excluded == 1
    # the excluded folder chain was looked up from Drive, once, to decide
    assert ("drive", "get", "fHooks") in noisy.calls


def test_incremental_removes_an_indexed_file_that_moves_into_an_excluded_folder(
    session, noisy
):
    rules = ex.parse_rules(["Pool photos"])
    di.full_sync(session, noisy.google(), exclude=rules)
    assert "log1" in {f.drive_id for f in session.exec(select(DriveFile)).all()}
    noisy.files["log1"]["parents"] = ["fPhotos"]
    noisy.record_change("log1")

    di.incremental_sync(session, noisy.google(), exclude=rules)

    assert "log1" not in {f.drive_id for f in session.exec(select(DriveFile)).all()}


def test_a_failed_exclusion_lookup_skips_that_file_and_keeps_going(
    session, noisy, monkeypatch
):
    rules = ex.parse_rules([".git"])
    di.full_sync(session, noisy.google(), exclude=rules)
    noisy.add("gitNew", "HEAD", "text/plain", "fHooks")
    noisy.record_change("gitNew")
    noisy.add("docNew", "New notes", DOC, "fDocs")
    noisy.documents["docNew"] = {"tabs": [doc_tab("T", doc_paragraph("fresh text"))]}
    noisy.record_change("docNew")

    def refuse(services, parent_id, folders, root_ids):
        if parent_id == "fHooks":
            raise RuntimeError("Drive unavailable")

    monkeypatch.setattr(di, "_learn_ancestors", refuse)

    report = di.incremental_sync(session, noisy.google(), exclude=rules)

    ids = {f.drive_id for f in session.exec(select(DriveFile)).all()}
    assert "gitNew" not in ids and "docNew" in ids
    assert report.text_errors == 1 and "exclude rules" in report.errors[0]
    assert session.get(SyncState, 1).last_error is None
