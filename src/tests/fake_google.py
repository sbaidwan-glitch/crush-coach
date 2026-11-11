"""A fake Google Drive, Docs and Sheets for the Drive index tests.

Everything in it is made up. It answers the handful of calls drive_index makes,
in the same shape as the real client (`service.files().list(**kw).execute()`).
"""

from googleapiclient.errors import HttpError
from httplib2 import Response

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
PDF = "application/pdf"


def http_error(status: int, reason: str = "fake failure") -> HttpError:
    return HttpError(Response({"status": str(status)}), reason.encode(), uri="fake://")


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries: int = 0):
        return self._fn()


def make_pdf(*pages: str) -> bytes:
    """A tiny valid PDF with one line of text per page."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_id = 3 + 2 * len(pages)
    for i, page_text in enumerate(pages):
        content_id = 4 + 2 * i
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_id} 0 R "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
            ).encode()
        )
        stream = f"BT /F1 12 Tf 72 700 Td ({page_text}) Tj ET".encode()
        objects.append(
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return out


def doc_paragraph(text: str, style: str = "NORMAL_TEXT") -> dict:
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": [{"textRun": {"content": text + "\n"}}],
        }
    }


def doc_tab(title: str, *paragraphs: dict) -> dict:
    return {
        "tabProperties": {"title": title},
        "documentTab": {"body": {"content": list(paragraphs)}},
    }


class FakeGoogle:
    """Holds a made-up drive and hands out the three service objects."""

    def __init__(self):
        self.drives: dict[str, str] = {}
        self.files: dict[str, dict] = {}  # id -> Drive file resource
        self.documents: dict[str, dict] = {}  # id -> Docs API document
        self.spreadsheets: dict[str, list[tuple[str, list[list]]]] = {}
        self.media: dict[str, bytes] = {}
        self.broken: dict[str, Exception] = {}  # id -> error on reading its text
        self.changes: list[dict] = []
        self.invalid_tokens: set[str] = set()
        self.calls: list[tuple] = []  # (api, method, id) for reads
        self.fail_listing: Exception | None = None

    # --- building the drive ---

    def add_drive(self, drive_id: str, name: str) -> None:
        self.drives[drive_id] = name

    def add(self, file_id: str, name: str, mime: str, parent: str | None, **extra):
        resource = {
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "parents": [parent] if parent else [],
            "modifiedTime": extra.pop("modified", "2026-10-01T12:00:00.000Z"),
            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
            **extra,
        }
        self.files[file_id] = resource
        return resource

    def record_change(self, file_id: str, *, removed: bool = False) -> None:
        change = {"changeType": "file", "fileId": file_id, "removed": removed}
        if not removed:
            change["file"] = dict(self.files[file_id])
        self.changes.append(change)

    # --- the services ---

    @property
    def token(self) -> str:
        return f"t{len(self.changes)}"

    def google(self):
        from drive_index import GoogleServices

        return GoogleServices(_Drive(self), _Docs(self), _Sheets(self))


class _Drive:
    def __init__(self, fake: FakeGoogle):
        self.fake = fake

    def drives(self):
        fake = self.fake

        class Drives:
            def list(self, **kw):
                return _Request(
                    lambda: {
                        "drives": [{"id": i, "name": n} for i, n in fake.drives.items()]
                    }
                )

        return Drives()

    def files(self):
        fake = self.fake

        class Files:
            def list(self, **kw):
                assert kw.get("supportsAllDrives") is True
                assert kw.get("includeItemsFromAllDrives") is True
                assert kw.get("corpora") == "allDrives"

                def run():
                    if fake.fail_listing:
                        raise fake.fail_listing
                    return {"files": [dict(f) for f in fake.files.values()]}

                return _Request(run)

            def get(self, fileId, **kw):
                def run():
                    fake.calls.append(("drive", "get", fileId))
                    if fileId in fake.drives:
                        return {"id": fileId, "name": fake.drives[fileId]}
                    return {
                        key: value
                        for key, value in fake.files[fileId].items()
                        if key in ("id", "name", "parents", "mimeType")
                    }

                return _Request(run)

            def get_media(self, fileId, **kw):
                def run():
                    fake.calls.append(("drive", "get_media", fileId))
                    if fileId in fake.broken:
                        raise fake.broken[fileId]
                    return fake.media[fileId]

                return _Request(run)

        return Files()

    def changes(self):
        fake = self.fake

        class Changes:
            def getStartPageToken(self, **kw):
                return _Request(lambda: {"startPageToken": fake.token})

            def list(self, pageToken, **kw):
                assert kw.get("supportsAllDrives") is True
                assert kw.get("includeItemsFromAllDrives") is True

                def run():
                    if pageToken in fake.invalid_tokens:
                        raise http_error(410, "page token expired")
                    start = int(pageToken.removeprefix("t"))
                    return {
                        "changes": fake.changes[start:],
                        "newStartPageToken": fake.token,
                    }

                return _Request(run)

        return Changes()


class _Docs:
    def __init__(self, fake: FakeGoogle):
        self.fake = fake

    def documents(self):
        fake = self.fake

        class Documents:
            def get(self, documentId, includeTabsContent=False, **kw):
                assert includeTabsContent is True  # every tab must be read

                def run():
                    fake.calls.append(("docs", "get", documentId))
                    if documentId in fake.broken:
                        raise fake.broken[documentId]
                    return fake.documents[documentId]

                return _Request(run)

        return Documents()


class _Sheets:
    def __init__(self, fake: FakeGoogle):
        self.fake = fake

    def spreadsheets(self):
        fake = self.fake

        class Values:
            def batchGet(self, spreadsheetId, ranges, **kw):
                def run():
                    fake.calls.append(("sheets", "batchGet", spreadsheetId))
                    tabs = dict(fake.spreadsheets[spreadsheetId])
                    result = []
                    for r in ranges:
                        title = r[1:-1].replace("''", "'")
                        result.append({"range": r, "values": tabs[title]})
                    return {"valueRanges": result}

                return _Request(run)

        class Spreadsheets:
            def get(self, spreadsheetId, **kw):
                def run():
                    fake.calls.append(("sheets", "get", spreadsheetId))
                    if spreadsheetId in fake.broken:
                        raise fake.broken[spreadsheetId]
                    return {
                        "sheets": [
                            {"properties": {"title": title, "sheetType": "GRID"}}
                            for title, _ in fake.spreadsheets[spreadsheetId]
                        ]
                    }

                return _Request(run)

            def values(self):
                return Values()

        return Spreadsheets()
