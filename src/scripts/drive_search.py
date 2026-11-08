"""Search the team's shared Google Drive from the command line."""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# A script is imported relative to src/scripts, so add src for `import drive`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drive import DriveError, search


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    parser = argparse.ArgumentParser(description="Search the Crush shared drive")
    parser.add_argument("query", help="text to find in file names or document contents")
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="maximum number of results (default: 10)",
    )
    args = parser.parse_args()

    try:
        results = search(args.query, args.limit)
    except DriveError as exc:
        print(f"Drive search failed: {exc}", file=sys.stderr)
        return 1

    if not results:
        print("No matching files found.")
        return 0

    for result in results:
        print(result["title"])
        for location in result["locations"]:
            print(f"  Folder: {location['folder_path']}")
            print(f"    Modified: {location['modified_time']}")
            print(f"    Type: {location['mime_type']}")
            print(f"    Link: {location['link']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
