"""Save UI screenshots for the process log: `make process-shots NAME=home-redesign`.

    cd src && ../.venv/bin/python scripts/process_shots.py home-redesign [PAGE ...]

Runs `screenshots.py` (the same throwaway SQLite sample data and fake signed-in session as
`make screenshots`, so no real names appear) for the given pages, in the light theme at
1280px and 375px, and copies each picture to

    process/YYYY-MM-DD-<NAME>-<page>-<width>.png

with today's date in COACH_TIMEZONE. A PAGE is a path ("/tasks") or a name from
`screenshots.PAGES` ("tasks-member"); the default is "/" and "/tasks" (named home and tasks).
It checks every target first and refuses to overwrite a file: nothing is run or written if
one exists.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
PROCESS = ROOT / "process"
DEFAULT_PAGES = ("/", "/tasks")
NAME_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")

sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "scripts"))

import screenshots

import localtime


def targets(name: str, pages: list[str], day: str) -> dict[tuple[str, int], Path]:
    """(page name, width) -> where its picture goes in process/."""
    rows = screenshots.pick_pages(pages)
    return {
        (row[0], width): PROCESS / f"{day}-{name}-{row[0]}-{width}.png"
        for row in rows
        for width in screenshots.WIDTHS
    }


def main(argv: list[str]) -> int:
    if not argv:
        print("Usage: make process-shots NAME=home-redesign [PAGES=\"/ /tasks\"]")
        return 2
    name, pages = argv[0], argv[1:] or list(DEFAULT_PAGES)
    if not NAME_PATTERN.fullmatch(name):
        print(f"NAME must be lower-case words joined by hyphens (e.g. home-redesign), not {name!r}.")
        return 2

    day = localtime.today().isoformat()
    wanted = targets(name, pages, day)
    taken = [path for path in wanted.values() if path.exists()]
    if taken:
        print("Refusing to overwrite; these already exist (pick another NAME or delete them):")
        for path in taken:
            print(f"  {path.relative_to(ROOT)}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        shots = screenshots.__file__
        result = subprocess.run(
            [sys.executable, shots, "--scheme", "light", "--out", tmp, "--pages", *pages],
            cwd=SRC,
            check=False,
        )
        if result.returncode != 0:
            print("The screenshots script reported problems; nothing was copied.")
            return result.returncode
        PROCESS.mkdir(exist_ok=True)
        for (page, width), target in wanted.items():
            shutil.copyfile(Path(tmp) / f"{page}-{width}.png", target)
            print(f"saved {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
