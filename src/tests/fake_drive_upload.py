"""A fake Drive client for the one thing drive_write does with it: find or make the
"Notebook photos" folder and add JPEG files to it. Made-up ids; nothing reaches Google.

It supports only `files().list` and `files().create`, so a test that passes proves the app
asked for nothing else: no update, delete or move of anything that exists."""

from types import SimpleNamespace

from fake_google import _Request

WRITE_FOLDER = "folderWritable01"  # a placeholder: tests set DRIVE_WRITE_FOLDER_ID to it


class FakeDriveUpload:
    def __init__(self):
        self.files_made: list[dict] = []  # every file or folder created, in order
        self.uploads: list[dict] = []  # {"name", "parent", "bytes"} for each photo
        self.lists: list[dict] = []
        self.fail_create: Exception | None = None  # raised when a photo is uploaded
        self.fail_list: Exception | None = None
        self._next = 1
        self._folders: dict[tuple[str, str], str] = {}
        self.files = lambda: SimpleNamespace(list=self._list, create=self._create)

    @property
    def folders(self) -> list[dict]:
        return [f for f in self.files_made if f["kind"] == "folder"]

    def _list(self, **kwargs):
        self.lists.append(kwargs)
        assert kwargs.get("supportsAllDrives") is True
        assert kwargs.get("includeItemsFromAllDrives") is True

        def run():
            if self.fail_list:
                raise self.fail_list
            query = kwargs["q"]
            for (name, parent), found in self._folders.items():
                if f"name = '{name}'" in query and f"'{parent}' in parents" in query:
                    return {"files": [{"id": found}]}
            return {"files": []}

        return _Request(run)

    def _create(self, **kwargs):
        assert kwargs.get("supportsAllDrives") is True

        def run():
            body = kwargs["body"]
            made_id = f"drv{self._next:03d}"
            self._next += 1
            parent = body["parents"][0]
            if body.get("mimeType") == "application/vnd.google-apps.folder":
                self._folders[(body["name"], parent)] = made_id
                self.files_made.append({"kind": "folder", "name": body["name"], "parent": parent, "id": made_id})
                return {"id": made_id}
            if self.fail_create:
                raise self.fail_create
            media = kwargs["media_body"]
            data = media.getbytes(0, media.size())
            self.uploads.append({"name": body["name"], "parent": parent, "bytes": data, "id": made_id})
            self.files_made.append({"kind": "file", "name": body["name"], "parent": parent, "id": made_id})
            return {"id": made_id, "webViewLink": f"https://drive.google.com/file/d/{made_id}/view"}

        return _Request(run)
