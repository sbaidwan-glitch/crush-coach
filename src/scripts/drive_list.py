"""List what is inside a folder of the team's shared Google Drive.

    cd src && ../.venv/bin/python scripts/drive_list.py "Mechanical/CAD"
    cd src && ../.venv/bin/python scripts/drive_list.py <folder id or folder link>

If a path matches several folders, their ids and full paths are printed so you can
list one by id.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# A script is imported relative to src/scripts, so add src for `import drive`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drive import DriveError, FolderNotFound, list_folder


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    parser = argparse.ArgumentParser(description="List a Crush shared-drive folder")
    parser.add_argument("folder", help="folder path (Mechanical/CAD), id, or link")
    parser.add_argument(
        "--limit", type=int, default=100, help="max items (default 100)"
    )
    args = parser.parse_args()

    try:
        listing = list_folder(args.folder, args.limit)
    except FolderNotFound as exc:
        print(f"Not found: {exc}", file=sys.stderr)
        return 1
    except DriveError as exc:
        print(f"Drive failed: {exc}", file=sys.stderr)
        return 1

    if listing["candidates"]:
        print("Several folders match. List one by id:")
        for candidate in listing["candidates"]:
            print(f"  {candidate['folder_path']}")
            print(f"    id: {candidate['id']}  modified: {candidate['modified_time']}")
        return 0

    print(f"{listing['folder_path']}  (id {listing['folder_id']})")
    if not listing["entries"]:
        print("  (empty)")
    for entry in listing["entries"]:
        print(f"  [{entry['kind']}] {entry['name']}")
        print(f"      modified {entry['modified_time'][:10]}  id {entry['id']}")
        print(f"      {entry['link']}")
    if listing["total"] > len(listing["entries"]):
        print(f"  … showing {len(listing['entries'])} of {listing['total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
