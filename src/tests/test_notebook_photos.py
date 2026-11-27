"""Photos on a notebook entry: what the server accepts (the real file type, a size cap, no
location data), who may see them, and how publishing sends them to Drive. Made-up images; the
Drive client is a fake that can only list and create."""

import io

import pytest
from fake_crush_doc import (
    DOC_ID,  # noqa: F401  (the `doc` fixture sets DECISIONS_DOC_ID)
)
from fake_drive_upload import WRITE_FOLDER, FakeDriveUpload
from fake_google import http_error
from fastapi.testclient import TestClient
from notebook_helpers import HTMX, LEAD, MEMBER, OTHER, add_task, stored
from PIL import Image
from sqlmodel import select

import drive_write
import main
import photos
from models import DriveWrite, NotePhoto, WorkNote

# --- the picture checker on its own -------------------------------------------------------------


def picture(size=(800, 600), color=(200, 30, 30), fmt="JPEG", **save):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt, **save)
    return buffer.getvalue()


def with_location():
    """A JPEG that says where it was taken, with what, and sideways."""
    exif = Image.Exif()
    exif[0x0112] = 6  # rotated: the photo is turned upright, then the tag is dropped
    exif[0x010F] = "SecretCam"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (34.0, 6.0, 0.0), "W", (117.0, 42.0, 0.0)
    return picture((3000, 2000), exif=exif)


def exif_of(data):
    return Image.open(io.BytesIO(data)).getexif()


def test_location_data_and_camera_details_are_stripped():
    original = with_location()
    assert exif_of(original).get_ifd(0x8825)  # the test photo really has GPS

    kept = photos.clean(original, "site.jpg")

    assert len(exif_of(kept.data)) == 0 and not exif_of(kept.data).get_ifd(0x8825)
    assert b"SecretCam" not in kept.data and b"Exif" not in kept.data
    assert Image.open(io.BytesIO(kept.data)).format == "JPEG"


def test_the_photo_is_turned_upright_and_shrunk_to_1600_px():
    kept = photos.clean(with_location())

    assert (kept.width, kept.height) == (1067, 1600)  # 3000x2000 turned on its side, longer side 1600


def test_a_big_png_becomes_a_small_jpeg_and_transparency_goes_white():
    buffer = io.BytesIO()
    Image.new("RGBA", (2400, 1200), (0, 0, 0, 0)).save(buffer, "PNG")

    kept = photos.clean(buffer.getvalue())

    assert max(kept.width, kept.height) == 1600 and len(kept.data) <= photos.MAX_BYTES
    assert Image.open(io.BytesIO(kept.data)).convert("RGB").getpixel((5, 5))[0] > 240


def test_webp_is_accepted():
    assert photos.clean(picture(fmt="WEBP")).data[:3] == b"\xff\xd8\xff"


@pytest.mark.parametrize(
    "raw, name",
    [
        (b"#!/bin/sh\nrm -rf /\n", "photo.jpg"),  # a script with a picture's name
        (b"%PDF-1.4 not a photo", "scan.png"),
        (b"<html><script>alert(1)</script></html>", "x.jpeg"),
        (b"GIF89a" + b"\x00" * 20, "x.gif"),
        (b"", "empty.jpg"),
        (b"\xff\xd8\xff\xe0" + b"junk" * 50, "truncated.jpg"),  # starts like a JPEG, isn't one
    ],
)
def test_what_is_not_really_a_photo_is_refused_whatever_it_is_called(raw, name):
    with pytest.raises(photos.PhotoError):
        photos.clean(raw, name)


def test_a_gif_is_refused_even_though_it_is_a_picture():
    buffer = io.BytesIO()
    Image.new("P", (10, 10)).save(buffer, "GIF")

    with pytest.raises(photos.PhotoError, match="isn’t a photo"):
        photos.clean(buffer.getvalue(), "x.gif")


def test_a_file_over_1_5_mb_is_refused_before_it_is_opened():
    with pytest.raises(photos.PhotoError, match="over 1.5 MB"):
        photos.clean(b"\xff\xd8\xff" + b"0" * photos.MAX_BYTES, "big.jpg")


def test_a_picture_that_unpacks_to_something_huge_is_refused():
    buffer = io.BytesIO()
    Image.new("1", (12000, 12000)).save(buffer, "PNG")  # tiny file, 144 million pixels
    assert len(buffer.getvalue()) < photos.MAX_BYTES

    with pytest.raises(photos.PhotoError):
        photos.clean(buffer.getvalue(), "bomb.png")


def test_noise_that_cannot_be_squeezed_under_the_cap_is_refused_or_fits():
    import os

    noisy = Image.frombytes("RGB", (1600, 1200), os.urandom(1600 * 1200 * 3))
    buffer = io.BytesIO()
    noisy.save(buffer, "PNG")
    raw = buffer.getvalue()

    if len(raw) > photos.MAX_BYTES:  # a PNG of noise is far bigger than the cap
        with pytest.raises(photos.PhotoError):
            photos.clean(raw)


# --- posting photos --------------------------------------------------------------------------------


def upload(client, *named, action="photos", **data):
    files = [("photos", (name, raw, "image/jpeg")) for name, raw in named]
    data = {"task": "M06", "did": "", "why": "", **data}
    return client.post(f"/notebook/entry/{action}", data=data, files=files or None, headers=HTMX)


def rows(session):
    session.expire_all()
    return list(session.exec(select(NotePhoto).order_by(NotePhoto.position)).all())


def test_a_photo_alone_makes_a_draft_and_is_stored_clean(session, as_user):
    add_task(session)

    response = upload(as_user(MEMBER), ("site.jpg", with_location()))

    assert response.status_code == 200 and "1 photo added." in response.text
    (note,) = stored(session, WorkNote)
    assert note.state == "draft" and note.did == ""
    (photo,) = rows(session)
    assert (photo.note_id, photo.member, photo.content_type) == (note.id, "sam@example.edu", "image/jpeg")
    assert len(exif_of(photo.data)) == 0 and photo.size == len(photo.data) <= photos.MAX_BYTES
    assert f'src="/notebook/photos/{photo.id}"' in response.text and "Remove" in response.text


def test_the_phone_button_opens_the_camera_and_the_other_picks_files(session, as_user):
    add_task(session)

    html = as_user(MEMBER).get("/notebook/panel").text

    assert 'accept="image/*" capture="environment"' in html  # "Add photos": the camera
    assert 'accept="image/*" multiple' in html and "Choose from files" in html


def test_an_entry_holds_six_photos_and_the_seventh_is_refused_with_a_reason(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    first = upload(client, *[(f"{n}.jpg", picture(color=(n * 30, 10, 10))) for n in range(5)])
    entry = stored(session, WorkNote)[0].id

    second = upload(client, ("5.jpg", picture()), ("6.jpg", picture()), entry=str(entry))

    assert "5 photos added." in first.text
    assert "1 photo added." in second.text and "up to 6 photos" in second.text and "“6.jpg” wasn't added" in second.text.replace("&#39;", "'")
    assert len(rows(session)) == 6
    assert [p.position for p in rows(session)] == [0, 1, 2, 3, 4, 5]


def test_one_bad_file_does_not_stop_the_good_ones_and_is_named(session, as_user):
    add_task(session)

    response = upload(as_user(MEMBER), ("fine.jpg", picture()), ("evil.jpg", b"<script>alert(1)</script>"))

    assert len(rows(session)) == 1
    assert "evil.jpg" in response.text and "isn’t a photo" in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_an_oversize_upload_is_refused_without_keeping_it(session, as_user):
    add_task(session)

    response = upload(as_user(MEMBER), ("huge.jpg", b"\xff\xd8\xff" + b"0" * (photos.MAX_BYTES + 10)))

    assert rows(session) == [] and "over 1.5 MB" in response.text


def test_a_request_far_bigger_than_six_photos_is_refused_up_front(session, as_user):
    add_task(session)

    response = as_user(MEMBER).post(
        "/notebook/entry/save", content=b"x", headers={"content-length": str(50_000_000), "content-type": "multipart/form-data; boundary=b"}
    )

    assert response.status_code == 413 and stored(session, WorkNote) == []


def test_removing_a_photo_from_a_draft(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    upload(client, ("a.jpg", picture()), ("b.jpg", picture(color=(1, 2, 3))))
    keep, drop = rows(session)
    entry = stored(session, WorkNote)[0].id

    response = client.post(
        f"/notebook/entry/remove-photo-{drop.id}", data={"task": "M06", "entry": str(entry), "did": "typed text"}, headers=HTMX
    )

    assert "Photo removed." in response.text and [p.id for p in rows(session)] == [keep.id]
    assert stored(session, WorkNote)[0].did == "typed text"  # what they had typed is kept


def test_discarding_a_draft_takes_its_photos_with_it(session, as_user):
    add_task(session)
    client = as_user(MEMBER)
    upload(client, ("a.jpg", picture()))
    entry = stored(session, WorkNote)[0].id

    client.post("/notebook/entry/discard", data={"task": "M06", "entry": str(entry)}, headers=HTMX)

    assert rows(session) == [] and stored(session, WorkNote) == []


def test_photos_work_in_a_browser_without_htmx_too(session, as_user):
    add_task(session)

    response = as_user(MEMBER).post(
        "/notebook/entry/photos", data={"task": "M06"}, files=[("photos", ("a.jpg", picture(), "image/jpeg"))],
        follow_redirects=False,
    )

    assert response.status_code == 303 and len(rows(session)) == 1


def test_the_form_can_not_be_used_to_attach_a_photo_to_someone_elses_draft(session, as_user):
    add_task(session)
    upload(as_user(MEMBER), ("a.jpg", picture()))
    entry = stored(session, WorkNote)[0].id

    response = upload(as_user(OTHER), ("b.jpg", picture()), entry=str(entry))

    assert response.status_code == 404 and len(rows(session)) == 1


# --- who may see a photo ----------------------------------------------------------------------------


def test_a_draft_photo_is_served_to_its_member_only(session, as_user):
    add_task(session)
    upload(as_user(MEMBER), ("a.jpg", picture()))
    (photo,) = rows(session)
    url = f"/notebook/photos/{photo.id}"

    mine = as_user(MEMBER).get(url)
    assert mine.status_code == 200 and mine.headers["content-type"] == "image/jpeg"
    assert mine.content == photo.data
    assert "no-store" in mine.headers["cache-control"] and mine.headers["x-content-type-options"] == "nosniff"
    for stranger in (OTHER, LEAD):
        assert as_user(stranger).get(url).status_code == 404, stranger["name"]
    assert as_user(OTHER).get("/notebook/photos/99999").status_code == 404  # same as not existing


def test_a_published_entrys_photos_are_open_to_signed_in_members(session, as_user, doc):
    add_task(session)
    client = as_user(MEMBER)
    upload(client, ("a.jpg", picture()))
    entry = stored(session, WorkNote)[0].id
    client.post("/notebook/entry/publish", data={"task": "M06", "entry": str(entry), "did": "Welded."}, headers=HTMX)
    (photo,) = rows(session)

    for viewer in (OTHER, LEAD):
        response = as_user(viewer).get(f"/notebook/photos/{photo.id}")
        assert response.status_code == 200 and response.content == photo.data
    # ...and the Record links to it for any member.
    assert f"/notebook/photos/{photo.id}" in as_user(OTHER).get("/record?notes=1").text


def test_signed_out_visitors_get_no_photo(session, as_user, monkeypatch):
    add_task(session)
    upload(as_user(MEMBER), ("a.jpg", picture()))
    (photo,) = rows(session)
    monkeypatch.undo()
    main.app.dependency_overrides.clear()

    with TestClient(main.app) as visitor:
        response = visitor.get(f"/notebook/photos/{photo.id}", follow_redirects=False)

    assert response.status_code == 303 and "/login" in response.headers["location"]


# --- publishing: photos go to Drive first, the text always goes ------------------------------------------


@pytest.fixture
def drive_up(monkeypatch):
    service = FakeDriveUpload()
    monkeypatch.setenv("DRIVE_WRITE_FOLDER_ID", WRITE_FOLDER)

    def no_network():
        raise AssertionError("a test reached for the real Google Drive client")

    monkeypatch.setattr(drive_write, "build_drive", lambda: service)
    return service


def publish_with_photos(client, n=2, **data):
    upload(client, *[(f"{i}.jpg", picture(color=(i * 40, 5, 5))) for i in range(n)])
    entry = stored_entry()
    return client.post(
        "/notebook/entry/publish", data={"task": "M06", "entry": str(entry), "did": "Welded the corners.", **data}, headers=HTMX
    )


def stored_entry():
    from sqlmodel import Session

    import db

    with Session(db.engine) as s:
        return s.exec(select(WorkNote)).first().id


def test_publishing_uploads_the_photos_to_the_notebook_photos_folder_and_links_them_in_the_doc(
    session, as_user, doc, drive_up
):
    add_task(session)

    response = publish_with_photos(as_user(MEMBER))

    assert "Published. Written to the Enclosure tab" in response.text and "stayed in the app" not in response.text
    (folder,) = drive_up.folders
    assert (folder["name"], folder["parent"]) == ("Notebook photos", WRITE_FOLDER)
    assert [u["parent"] for u in drive_up.uploads] == [folder["id"], folder["id"]]
    assert all(u["name"].startswith("M06-entry") and u["name"].endswith(".jpg") for u in drive_up.uploads)
    assert all(u["bytes"][:3] == b"\xff\xd8\xff" for u in drive_up.uploads)  # the cleaned JPEG
    lines = doc.lines("Enclosure")
    assert lines[-2].startswith("Sam Example · M06: Welded the corners.")
    assert lines[-1] == "Photos: Photo 1, Photo 2"
    links = doc.links("Enclosure")  # link -> the words carrying it
    assert sorted(links.values()) == ["Photo 1", "Photo 2"]
    assert all(link.startswith("https://drive.google.com/file/d/") for link in links)
    saved = rows(session)
    assert all(p.drive_link.startswith("https://drive.google.com/file/d/") for p in saved)
    assert {w.kind for w in stored(session, DriveWrite)} == {"note", "photo"}


def test_only_lists_and_creates_ever_reach_drive_and_the_folder_is_made_once(session, as_user, doc, drive_up):
    add_task(session)
    client = as_user(MEMBER)
    publish_with_photos(client)
    second = add_task(session, "M07", title="Second")
    assert second is not None
    upload(client, ("c.jpg", picture()), task="M07")
    entry = max(n.id for n in stored(session, WorkNote))
    client.post("/notebook/entry/publish", data={"task": "M07", "entry": str(entry), "did": "Another."}, headers=HTMX)

    assert len(drive_up.folders) == 1  # found the second time, not made again
    assert {f["kind"] for f in drive_up.files_made} == {"folder", "file"}


def test_publishing_twice_uploads_each_photo_once(session, as_user, doc, drive_up):
    add_task(session)
    client = as_user(MEMBER)
    publish_with_photos(client)
    entry = stored(session, WorkNote)[0].id

    client.post("/notebook/entry/publish", data={"task": "M06", "entry": str(entry), "did": "Welded the corners."}, headers=HTMX)

    assert len(drive_up.uploads) == 2
    assert doc.tab("Enclosure").text().count("Photos: Photo 1, Photo 2") == 1
    assert len([w for w in stored(session, DriveWrite) if w.kind == "photo"]) == 2


def test_a_failed_photo_upload_still_publishes_the_text_and_says_the_photos_stayed(
    session, as_user, doc, drive_up
):
    add_task(session)
    drive_up.fail_create = http_error(403, "no access secret-token-9")

    response = publish_with_photos(as_user(MEMBER))

    html = response.text
    assert "Published. Written to the Enclosure tab" in html
    assert "The 2 photos stayed in the app, not in Drive." in html
    assert "Share the folder with the app" in html and "secret-token-9" not in html
    assert doc.lines("Enclosure")[-1].startswith("Sam Example · M06: Welded the corners.")
    assert not any(line.startswith("Photos:") for line in doc.lines("Enclosure"))
    assert all(p.drive_link is None for p in rows(session))
    assert {w.status for w in stored(session, DriveWrite) if w.kind == "photo"} == {"failed"}
    note = stored(session, WorkNote)[0]
    assert note.state == "published"
    # The entry says so on a later visit, and its photos are still viewable in the app.
    later = as_user(MEMBER).get("/notebook/panel?tab=earlier").text
    assert "2 photos stayed in the app." in later and "/notebook/photos/" in later


def test_without_a_drive_folder_the_photos_stay_in_the_app_and_the_text_is_published(
    session, as_user, doc, monkeypatch
):
    add_task(session)
    monkeypatch.delenv("DRIVE_WRITE_FOLDER_ID", raising=False)

    def no_drive():
        raise AssertionError("Drive was used with no folder set")

    monkeypatch.setattr(drive_write, "build_drive", no_drive)

    response = publish_with_photos(as_user(MEMBER), n=1)

    assert "The 1 photo stayed in the app" in response.text and "DRIVE_WRITE_FOLDER_ID" in response.text
    assert "Written to the Enclosure tab" in response.text


def test_a_failed_text_write_keeps_the_uploaded_photos_and_retry_adds_their_links_once(
    session, as_user, doc, drive_up
):
    add_task(session)
    doc.fail_get = http_error(503, "unavailable")
    client = as_user(MEMBER)
    failed = publish_with_photos(client)
    assert "didn’t reach the Crush document" in failed.text
    assert len(drive_up.uploads) == 2  # the photos did get there
    doc.fail_get = None
    entry = stored(session, WorkNote)[0].id

    retried = client.post(f"/notebook/entries/{entry}/retry", headers=HTMX)

    assert "Published. Written to the Enclosure tab" in retried.text
    assert len(drive_up.uploads) == 2  # not uploaded again
    assert doc.lines("Enclosure")[-1] == "Photos: Photo 1, Photo 2"


def test_an_unreachable_drive_never_breaks_publishing(session, as_user, doc, monkeypatch):
    add_task(session)
    monkeypatch.setenv("DRIVE_WRITE_FOLDER_ID", WRITE_FOLDER)

    def unreachable():
        raise RuntimeError("password=hunter2")

    monkeypatch.setattr(drive_write, "build_drive", unreachable)

    response = publish_with_photos(as_user(MEMBER), n=1)

    assert response.status_code == 200 and "hunter2" not in response.text
    assert "Written to the Enclosure tab" in response.text and "stayed in the app" in response.text


def test_the_photo_scope_is_narrow_and_lives_only_in_drive_write():
    assert drive_write.DRIVE_FILE_SCOPE == "https://www.googleapis.com/auth/drive.file"
    from pathlib import Path

    for path in Path(__file__).resolve().parents[1].glob("*.py"):
        if path.name != "drive_write.py":
            assert "auth/drive.file" not in path.read_text(), path.name
            assert "auth/documents\"" not in path.read_text(), path.name
