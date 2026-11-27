#!/usr/bin/env python3
"""PostToolUse hook: ruff-fix a Python file under src/ after Claude changed it.

Runs `.venv/bin/ruff check --fix <file>`. Findings it could not fix are handed
back to Claude as context; the hook never blocks (always exits 0). Standard
library only.
"""

import json
import os
import subprocess
import sys


def main() -> int:
    try:
        call = json.load(sys.stdin)
    except ValueError:
        return 0
    target = (call.get("tool_input") or {}).get("file_path")
    if not target or not target.endswith(".py"):
        return 0

    root = os.environ.get("CLAUDE_PROJECT_DIR") or call.get("cwd") or os.getcwd()
    if not os.path.isabs(target):
        target = os.path.join(call.get("cwd") or root, target)
    target = os.path.realpath(target)
    src = os.path.realpath(os.path.join(root, "src"))
    ruff = os.path.join(root, ".venv", "bin", "ruff")
    if not target.startswith(src + os.sep) or not os.path.isfile(target):
        return 0
    if not os.path.exists(ruff):
        return 0

    result = subprocess.run(
        [ruff, "check", "--fix", target],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    findings = (result.stdout + result.stderr).strip()
    if result.returncode != 0 and findings:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": f"ruff found problems it could not fix:\n{findings}",
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
