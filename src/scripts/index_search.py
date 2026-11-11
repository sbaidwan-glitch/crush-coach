"""Search the local Drive index by hand: what the coach will see.

cd src && ../.venv/bin/python scripts/index_search.py "lid latch"
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# A script is imported relative to src/scripts, so add src for `import db`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from sqlalchemy import func, select
from sqlmodel import Session

import db
import drive_index
from models import DriveFile, SyncState


def main() -> int:
    parser = argparse.ArgumentParser(description="Search the local Drive index")
    parser.add_argument("query", help="keywords, e.g. 'lid latch'")
    parser.add_argument("--limit", type=int, default=10, help="results (default: 10)")
    parser.add_argument(
        "--full-text",
        action="store_true",
        help="print each whole chunk instead of a short excerpt",
    )
    args = parser.parse_args()

    try:
        with Session(db.engine) as session:
            hits = drive_index.search(session, args.query, args.limit)
            state = session.get(SyncState, 1)
            indexed = session.scalar(select(func.count()).select_from(DriveFile))
    except Exception as exc:  # noqa: BLE001  (one clean line, not a traceback)
        print(
            f"Index search failed: {type(exc).__name__}. Is DATABASE_URL set?",
            file=sys.stderr,
        )
        return 1

    if not indexed:
        print("The index is empty. Run: python -m scripts.sync_drive --full")
        return 0
    if not hits:
        print("No matches.")
        return 0

    synced = (
        max(
            (t for t in (state.last_full_sync, state.last_incremental_sync) if t),
            default=None,
        )
        if state
        else None
    )
    print(f"{len(hits)} result(s); index last synced {synced or 'never'} UTC\n")
    for number, hit in enumerate(hits, start=1):
        modified = hit.modified_time.strftime("%Y-%m-%d") if hit.modified_time else "?"
        print(f"{number}. {hit.name}  [{hit.kind}, score {hit.score:.3f}]")
        print(f"   {hit.folder_path}  ·  modified {modified}")
        if hit.heading:
            print(f"   Section: {hit.heading}")
        print(f"   {hit.web_link}")
        if hit.text:
            print(
                "   "
                + (hit.text if args.full_text else hit.snippet).replace("\n", "\n   ")
            )
        else:
            print("   (matched by name/folder; no text)")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
