"""See how a Google Sheet is read into the index, tab by tab (real Google, read-only).

    cd src && ../.venv/bin/python scripts/sheet_preview.py "Master Task Spreadsheet"
    cd src && ../.venv/bin/python scripts/sheet_preview.py "Master Task Spreadsheet" --tab Master --lines 6

For each tab it prints which sheet row was taken as the header (or that none
qualified, so columns are lettered), the header labels, and the first lines the
index would store. Nothing is written to the database. This prints real sheet
contents to your terminal: don't paste them anywhere public.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# A script is imported relative to src/scripts, so add src for `import db`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from sqlalchemy import select
from sqlmodel import Session

import db
import drive_index
from models import DriveFile


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview how a sheet is indexed")
    parser.add_argument("name", help="the spreadsheet's name, as in the index")
    parser.add_argument("--tab", help="only this tab")
    parser.add_argument("--lines", type=int, default=4, help="lines to show per tab")
    args = parser.parse_args()

    try:
        with Session(db.engine) as session:
            files = session.execute(
                select(DriveFile.drive_id, DriveFile.name, DriveFile.folder_path)
                .where(DriveFile.kind == "sheet", DriveFile.name == args.name)
                .order_by(DriveFile.modified_time.desc())
            ).all()
        if not files:
            print(f"No indexed spreadsheet is named {args.name!r}.", file=sys.stderr)
            return 1
        drive_id, _name, folder = files[0]
        if len(files) > 1:
            print(f"{len(files)} sheets have that name; using the newest ({folder}).\n")
        services = drive_index.GoogleServices.from_environment()
        meta = drive_index._run(
            services.sheets.spreadsheets().get(
                spreadsheetId=drive_id, fields="sheets.properties(title,sheetType)"
            )
        )
        titles = [
            sheet["properties"]["title"]
            for sheet in meta.get("sheets", [])
            if sheet["properties"].get("sheetType", "GRID") == "GRID"
        ]
        for title in titles:
            if args.tab and title != args.tab:
                continue
            response = drive_index._run(
                services.sheets.spreadsheets()
                .values()
                .batchGet(
                    spreadsheetId=drive_id,
                    ranges=[drive_index._quote_sheet_title(title)],
                )
            )
            rows = response["valueRanges"][0].get("values", [])
            trace: list[tuple[int, str]] = []
            lines = drive_index.sheet_lines(rows, trace)
            print(f"=== tab {title!r}: {len(rows)} rows, {len(lines)} indexed lines")
            if not trace:
                print("  no row looked like a header: columns are labeled A, B, C...")
            for number, _ in trace:
                labels = [str(c) for c in rows[number - 1] if str(c).strip()]
                print(f"  header = sheet row {number}: {labels}")
            for line in lines[: args.lines]:
                print(f"  | {line[:230]}")
            print()
    except drive_index.DriveError as exc:
        print(f"Google Drive is not set up: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001  (one clean line, not a traceback)
        print(
            f"Could not preview the sheet: {drive_index.describe_error(exc)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
