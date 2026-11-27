"""`make process-shots`: file names, and refusing to overwrite (the browser part isn't run)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import process_shots


def test_default_pages_become_dated_files_per_width(monkeypatch):
    monkeypatch.setattr(process_shots, "PROCESS", Path("/repo/process"))

    wanted = process_shots.targets("home-redesign", list(process_shots.DEFAULT_PAGES), "2026-11-26")

    assert sorted(path.name for path in wanted.values()) == [
        "2026-11-26-home-redesign-home-1280.png",
        "2026-11-26-home-redesign-home-375.png",
        "2026-11-26-home-redesign-tasks-1280.png",
        "2026-11-26-home-redesign-tasks-375.png",
    ]


def test_a_page_can_be_named_instead_of_a_path(monkeypatch):
    monkeypatch.setattr(process_shots, "PROCESS", Path("/repo/process"))

    wanted = process_shots.targets("x", ["inbox"], "2026-11-26")

    assert {page for page, _ in wanted} == {"inbox"}


def test_an_unknown_page_stops_with_the_list():
    with pytest.raises(SystemExit):
        process_shots.targets("x", ["/nope"], "2026-11-26")


def test_it_refuses_to_overwrite_and_runs_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(process_shots, "PROCESS", tmp_path)
    monkeypatch.setattr(process_shots, "ROOT", tmp_path)
    monkeypatch.setattr(process_shots.localtime, "today", lambda: __import__("datetime").date(2026, 11, 26))
    (tmp_path / "2026-11-26-redo-home-375.png").write_bytes(b"keep me")

    def must_not_run(*args, **kwargs):
        raise AssertionError("screenshots must not run when a target exists")

    monkeypatch.setattr(process_shots.subprocess, "run", must_not_run)

    assert process_shots.main(["redo"]) == 1
    assert (tmp_path / "2026-11-26-redo-home-375.png").read_bytes() == b"keep me"
    assert "Refusing to overwrite" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["Home", "a_b", "../x", "-x", ""])
def test_a_bad_name_is_refused(name, capsys):
    assert process_shots.main([name]) == 2
