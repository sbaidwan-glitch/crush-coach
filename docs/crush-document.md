# The Crush document (write-back)

The Crush document is one Google Doc where the app **appends** two kinds of entry, so the
team's running log lives in Drive as well as in the app:

- a member's **notebook entry** (published from the Notebook, no lead needed): `Name · M06: what. Why: …`, then a line per spec they accepted and a link to each photo
- a **recorded answer**, written when a lead records it: a bold statement, why, what it
  replaces, sources, who recorded it.

All of it is in `src/drive_write.py`, the only module with a Google write scope
(`https://www.googleapis.com/auth/documents`, and `https://www.googleapis.com/auth/drive.file` for photos).

## Photos

Before an entry is written, its photos are uploaded to a **"Notebook photos"** folder (made on
first use) inside the folder named by `DRIVE_WRITE_FOLDER_ID`. Share that folder with the service
account as **Editor**. The `drive.file` scope lets the app touch only files it made. Each photo
uploads once (a `drive_write` row of kind `photo`). If an upload fails, or the variable is unset,
the text is still published and the page says the photos stayed in the app.

## Set it up

1. Make a Google Doc with one **tab** per part of Crush: `Enclosure`, `Seals`, `Thrusters`,
   `Actuators`, and a `General` tab. (A subsystem finds its tab by name: `Seals/Waterproofing`
   → `Seals`; `Gripper` and `Torpedo` → `Actuators`; `Electronics Mounts` → `Electronics
   Mounts` or `Enclosure`. When none matches, the entry goes under `General` and the app says
   so. With no `General` tab either, the write fails and can be retried after you add one.)
2. Share the doc with the service account's email as **Editor**. (The Drive reads are
   read-only; this is the one place the app writes.)
3. Set `DECISIONS_DOC_ID` to the id in the doc's address (`…/document/d/<id>/edit`) on **the
   web service and on the Drive sync cron service**. The id is configuration, never committed.
   The Docs scope is requested only for this write; nothing else in the app can write.

Without `DECISIONS_DOC_ID` the app still works: entries stay in the app (notebook, History,
Record) and say "The Crush document isn't set up yet", with a retry once it is.

## What gets written, and where

Under the subsystem's tab, below that day's date heading (`17 Nov 2026`, in the team's time
zone; the heading is created when the day has none). Each entry is added at the end of its
day's section. Nothing already in the document is edited or deleted: the code sends only
inserts and formatting for the text it inserts.

A `drive_write` row per entry makes a write happen once. A repeated request, a double click
or a retry never adds a second copy, and text already under the day's heading isn't added
again. If Google fails, the entry stays in the app with **Retry write to Drive**: members
retry their own entries in the Notebook (**Failed to publish**, **Retry**); leads retry answers on the Record.

## The Drive sync never reads it back

The Drive index skips the Crush document (`drive_exclude.py`, by `DECISIONS_DOC_ID`), so an
entry can't come back as new evidence against the answer it was written from (an answer's
"Replaces:" line even repeats the old value). **Set `DECISIONS_DOC_ID` on the sync cron
service too**, or the document would be indexed there. After setting it, a `--full` sync drops
a copy that was indexed earlier.

## Tests

`src/tests/fake_crush_doc.py` is a fake Docs client with the real index rules; the tests
(`test_drive_write.py`, `test_notebook.py`, `test_notebook_photos.py`, with `fake_drive_upload.py` for Drive) never touch Google and use a placeholder id.
