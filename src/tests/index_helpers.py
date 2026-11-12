"""Put made-up files straight into the Drive index, for tests of what reads it."""

from datetime import datetime

from models import DriveChunk, DriveFile

DOC_MIME = "application/vnd.google-apps.document"


def add_file(
    session,
    name,
    *,
    kind="doc",
    folder="MuddSub / 2025-2026 / Mechanical",
    modified="2026-10-05",
    chunks=(),
    drive_id=None,
    link=None,
):
    """Add one indexed file with one chunk per string in `chunks`. Returns the row."""
    drive_id = drive_id or f"id{name}".replace(" ", "")[:30].ljust(12, "x")
    row = DriveFile(
        drive_id=drive_id,
        name=name,
        mime_type=DOC_MIME if kind == "doc" else "application/octet-stream",
        folder_path=folder,
        modified_time=datetime.fromisoformat(modified),
        web_link=link or f"https://docs.google.com/document/d/{drive_id}/edit",
        kind=kind,
        text_status="ok" if chunks else "skipped",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    for position, text in enumerate(chunks):
        session.add(
            DriveChunk(file_id=row.id, position=position, heading=None, text=text)
        )
    session.commit()
    return row
