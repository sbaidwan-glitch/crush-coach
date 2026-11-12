"""What is in the local Drive index: counts, sizes, biggest files, what wasn't read.

    cd src && ../.venv/bin/python scripts/index_stats.py
    cd src && ../.venv/bin/python scripts/index_stats.py --noise   # what to exclude

Read-only. Prints names, paths and counts, never any file text.
"""

import argparse
import posixpath
import sys
from pathlib import Path

from dotenv import load_dotenv

# A script is imported relative to src/scripts, so add src for `import db`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from sqlalchemy import text
from sqlmodel import Session

import db


def table(rows, headers: list[str]) -> None:
    rows = [[str(cell) for cell in row] for row in rows]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows))
        if rows
        else len(headers[i])
        for i in range(len(headers))
    ]
    print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    for row in rows:
        print("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    if not rows:
        print("  (none)")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Stats for the local Drive index")
    parser.add_argument(
        "--noise",
        action="store_true",
        help="also show the biggest folders and file types (to pick exclusions)",
    )
    args = parser.parse_args()

    try:
        with Session(db.engine) as s:
            postgres = s.get_bind().dialect.name == "postgresql"

            print("Files by kind and text status (source size in MB)")
            table(
                s.execute(
                    text(
                        "select kind, text_status, count(*), "
                        "round(coalesce(sum(size), 0) / 1048576.0) "
                        "from drive_file group by 1, 2 order by 1, 2"
                    )
                ).all(),
                ["kind", "status", "files", "MB"],
            )

            files = s.execute(text("select count(*) from drive_file")).scalar()
            chunks = s.execute(text("select count(*) from drive_chunk")).scalar()
            print(f"Total: {files:,} files and folders, {chunks:,} text chunks")
            if postgres:
                size = s.execute(
                    text("select pg_database_size(current_database()) / 1048576.0")
                ).scalar()
                print(f"Database size: {size:.1f} MB")
                table(
                    s.execute(
                        text(
                            "select relname, round(pg_total_relation_size(oid) / 1048576.0, 1) "
                            "from pg_class where relname in "
                            "('drive_file', 'drive_chunk', 'sync_state') order by 2 desc"
                        )
                    ).all(),
                    ["table (with indexes)", "MB"],
                )
            else:
                print("Database size: only shown for Postgres\n")

            print("10 largest files by chunk count")
            table(
                s.execute(
                    text(
                        "select f.name, f.kind, count(*) as chunks, f.folder_path "
                        "from drive_chunk c join drive_file f on f.id = c.file_id "
                        "group by f.id order by chunks desc limit 10"
                    )
                ).all(),
                ["name", "kind", "chunks", "folder"],
            )

            print("Files not read (text_status other than ok), by kind and reason")
            table(
                s.execute(
                    text(
                        "select kind, text_status, "
                        "coalesce(regexp_replace(text_reason, '[0-9]+ MB', 'N MB'), '') "
                        "as reason, count(*) from drive_file "
                        "where text_status <> 'ok' group by 1, 2, 3 order by 4 desc"
                        if postgres
                        else "select kind, text_status, coalesce(text_reason, ''), "
                        "count(*) from drive_file where text_status <> 'ok' "
                        "group by 1, 2, 3 order by 4 desc"
                    )
                ).all(),
                ["kind", "status", "reason", "files"],
            )

            if args.noise:
                print("File types among 'other' files (by extension)")
                names = s.execute(
                    text("select name from drive_file where kind = 'other'")
                ).scalars()
                counts: dict[str, int] = {}
                for name in names:
                    ext = posixpath.splitext(name)[1].lower() or "(none)"
                    counts[ext] = counts.get(ext, 0) + 1
                table(
                    sorted(counts.items(), key=lambda item: -item[1])[:15],
                    ["extension", "files"],
                )

                print("Biggest folders by number of files directly inside")
                table(
                    s.execute(
                        text(
                            "select folder_path, count(*) as files, "
                            "sum(case when kind = 'other' then 1 else 0 end) as other "
                            "from drive_file where kind <> 'folder' "
                            "group by 1 order by 2 desc limit 15"
                        )
                    ).all(),
                    ["folder", "files", "of which other"],
                )

                print(
                    "Folder names that look like noise (counts include everything inside)"
                )
                table(
                    s.execute(
                        text(
                            "select split_part(f.name, '/', 1) as folder, count(c.id) as files "
                            "from drive_file f join drive_file c "
                            "on c.folder_path like f.folder_path || ' / ' || f.name || '%' "
                            "where f.kind = 'folder' and "
                            "(f.name ilike '.%' or f.name ilike any (array["
                            "'%photo%', '%image%', '%picture%', '%video%', '%log%', "
                            "'%github%', '%repo%', '%node_modules%', '%cache%', '%backup%'])) "
                            "group by 1 order by 2 desc limit 15"
                        )
                    ).all()
                    if postgres
                    else [],
                    ["folder name", "files inside"],
                )
    except Exception as exc:  # noqa: BLE001  (one clean line, not a traceback)
        print(
            f"Could not read the index: {type(exc).__name__}. Is DATABASE_URL set?",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
