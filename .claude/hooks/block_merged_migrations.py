#!/usr/bin/env python3
"""PreToolUse hook: refuse edits to a migration that is already on origin/main.

Reads the tool call as JSON on stdin. Exit 2 (message on stderr) blocks the
edit; exit 0 allows it. New migration files, which origin/main doesn't have yet,
stay editable. Standard library only.
"""

import json
import os
import subprocess
import sys

VERSIONS = "src/migrations/versions/"
MESSAGE = "Merged migrations are never edited; add a new one."


def git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", root, *args], capture_output=True, text=True, check=False
    )


def main() -> int:
    try:
        call = json.load(sys.stdin)
    except ValueError:
        return 0  # not a tool call we understand; never block on that
    target = (call.get("tool_input") or {}).get("file_path")
    if not target:
        return 0

    start = call.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    top = git(start, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return 0
    root = os.path.realpath(top.stdout.strip())
    if not os.path.isabs(target):
        target = os.path.join(start, target)
    relative = os.path.relpath(os.path.realpath(target), root)
    if relative.startswith("..") or not relative.startswith(VERSIONS):
        return 0

    if git(root, "rev-parse", "--verify", "--quiet", "origin/main").returncode != 0:
        print(
            "block_merged_migrations: origin/main not found (run `git fetch origin`); "
            "not checking this edit.",
            file=sys.stderr,
        )
        return 0
    if git(root, "cat-file", "-e", f"origin/main:{relative}").returncode == 0:
        print(f"{MESSAGE} ({relative} is on origin/main.)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
