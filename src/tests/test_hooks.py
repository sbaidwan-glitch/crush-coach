"""The Claude Code hook that refuses edits to merged migrations.

Each test builds a throwaway git repo with its own `origin/main`, so nothing
depends on this checkout's remotes (CI has no origin/main).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "block_merged_migrations.py"
MERGED = "src/migrations/versions/0001_merged.py"
NEW = "src/migrations/versions/0099_not_merged_yet.py"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    versions = tmp_path / "src" / "migrations" / "versions"
    versions.mkdir(parents=True)
    (tmp_path / MERGED).write_text("revision = '0001'\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "merged")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    (tmp_path / NEW).write_text("revision = '0099'\n")  # on disk, never committed
    return tmp_path


def run_hook(repo: Path, path: str, tool: str = "Edit"):
    call = {"tool_name": tool, "cwd": str(repo), "tool_input": {"file_path": path}}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(call),
        capture_output=True,
        text=True,
        cwd=repo,
        check=False,
    )


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
def test_a_migration_on_origin_main_is_refused(repo, tool):
    result = run_hook(repo, str(repo / MERGED), tool)
    assert result.returncode == 2
    assert "Merged migrations are never edited; add a new one." in result.stderr


def test_a_relative_path_to_a_merged_migration_is_refused_too(repo):
    assert run_hook(repo, MERGED).returncode == 2


def test_a_new_untracked_migration_stays_editable(repo):
    result = run_hook(repo, str(repo / NEW))
    assert result.returncode == 0
    assert result.stderr == ""


def test_a_migration_committed_on_the_branch_but_not_on_origin_main_stays_editable(repo):
    git(repo, "add", NEW)
    git(repo, "commit", "-q", "-m", "branch work")
    assert run_hook(repo, str(repo / NEW)).returncode == 0


def test_other_files_are_never_refused(repo):
    (repo / "src" / "app.py").write_text("x = 1\n")
    assert run_hook(repo, str(repo / "src" / "app.py")).returncode == 0


def test_without_origin_main_the_hook_warns_and_allows(repo):
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    result = run_hook(repo, str(repo / MERGED))
    assert result.returncode == 0
    assert "origin/main not found" in result.stderr
