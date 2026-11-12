"""The Drive index on real Postgres: full-text search, ranking, the GIN index.

Skipped unless TEST_POSTGRES_URL points at a throwaway database, e.g.

    docker run -d --rm -p 55432:5432 -e POSTGRES_USER=e190bo \\
        -e POSTGRES_PASSWORD=localdev -e POSTGRES_DB=app postgres:17
    TEST_POSTGRES_URL=postgresql://e190bo:localdev@localhost:55432/app make test

It creates the tables with the migrations and empties the Drive tables around
every test, so never point it at a database you care about.
"""

import os

import pytest
from fake_google import DOC, doc_paragraph, doc_tab
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, delete, select
from test_drive_index import NOW, season_docs

import db
import drive_index as di
import drive_rank
from models import DriveChunk, DriveFile, SyncState

URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_POSTGRES_URL is not set")


@pytest.fixture
def pg():
    url = URL.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    engine = create_engine(url)
    db.run_migrations(engine)

    def clear(s):
        for table in (DriveChunk, DriveFile, SyncState):
            s.exec(delete(table))
        s.commit()

    with Session(engine) as s:
        clear(s)
        yield s
        s.rollback()
        clear(s)
    engine.dispose()


def test_the_search_column_is_generated_and_gin_indexed(pg):
    indexes = {i["name"]: i for i in inspect(pg.get_bind()).get_indexes("drive_chunk")}
    assert (
        indexes["ix_drive_chunk_search_vector"]["dialect_options"]["postgresql_using"]
        == "gin"
    )
    column = pg.execute(
        text(
            "select is_generated from information_schema.columns "
            "where table_name = 'drive_chunk' and column_name = 'search_vector'"
        )
    ).scalar_one()
    assert column == "ALWAYS"


def test_sync_then_search_uses_full_text(pg, fake):
    di.full_sync(pg, fake.google())

    # the generated column was filled in for every chunk
    empty = pg.execute(
        text("select count(*) from drive_chunk where search_vector is null")
    ).scalar_one()
    assert empty == 0

    hits = di.search(pg, "lid latch")
    assert hits[0].name == "Lid latch design notes"  # name match beats content-only
    assert "Parts list" in {h.name for h in hits}  # its Lid latch row
    # any word matches, but the results with both words come first
    names = [h.name for h in hits]
    assert "Latch datasheet.pdf" in names  # only has "latch": still found
    # ...and a name match outranks a text-only match, even a full one
    assert names.index("Latch datasheet.pdf") < names.index("Parts list")
    assert "latch" in hits[0].snippet.lower()

    # stemming: "latches" finds "latch", "pressurizing" finds "Pressurize"
    assert di.search(pg, "latches")
    assert any(h.drive_id == "docLid" for h in di.search(pg, "pressurizing"))

    # websearch syntax: quoted phrase, exclusion, or
    assert di.search(pg, '"holds the enclosure lid"')[0].drive_id == "docLid"
    excluded = di.search(pg, "latch -gauge")  # the chunk mentioning a gauge is out
    assert all(h.chunk_id is None for h in excluded if h.drive_id == "docLid")
    assert di.search(pg, "gasket or gauge")


def test_cad_files_are_found_by_name_folder_and_kind_newest_first(pg, fake):
    di.full_sync(pg, fake.google())
    names = [h.name for h in di.search(pg, "where is the latest enclosure CAD")]
    assert names[:2] == ["Enclosure_v2.SLDPRT", "Enclosure_v1.SLDPRT"]
    assert [h.name for h in di.search(pg, "enclosure cad")][:2] == names[:2]
    assert di.search(pg, "SLDPRT")[0].kind == "cad"


def test_equal_scores_prefer_the_newer_file(pg, fake):
    fake.add("twinOld", "Twin plan", DOC, "fDocs", modified="2026-01-01T00:00:00Z")
    fake.add("twinNew", "Twin plan", DOC, "fDocs", modified="2026-06-01T00:00:00Z")
    for twin, extra in (("twinOld", "alpha"), ("twinNew", "bravo")):
        fake.documents[twin] = {
            "tabs": [doc_tab("T", doc_paragraph(f"gasket torque plan {extra}"))]
        }
    di.full_sync(pg, fake.google())
    hits = di.search(pg, "gasket torque")[:2]
    assert [h.drive_id for h in hits] == ["twinNew", "twinOld"]


def test_odd_text_and_queries_do_not_break_postgres(pg, fake):
    fake.documents["docLid"] = {
        "tabs": [doc_tab("T", doc_paragraph("nul\x00byte here"))]
    }
    di.full_sync(pg, fake.google())
    assert (
        pg.exec(select(DriveFile).where(DriveFile.drive_id == "docLid"))
        .one()
        .text_status
        == "ok"
    )
    for query in ("the", "a & b | c", "'", "\\", "(((", "latch:*", "x" * 5000):
        di.search(pg, query)  # must not raise


# --- names, types, copies on real full-text search ---


def add_cad(fake, file_id, name, folder="fCad", modified="2026-03-29T00:00:00Z"):
    fake.add(
        file_id, name, "application/sldworks", folder, modified=modified, size="900"
    )


def test_every_file_has_a_generated_full_text_entry_even_without_text(pg, fake):
    add_cad(fake, "latchCad", "6082A12_Draw Latch.SLDPRT")
    di.full_sync(pg, fake.google())

    empty = pg.execute(
        text("select count(*) from drive_file where search_vector is null")
    ).scalar_one()
    assert empty == 0
    assert (
        pg.execute(
            text(
                "select is_generated from information_schema.columns "
                "where table_name = 'drive_file' and column_name = 'search_vector'"
            )
        ).scalar_one()
        == "ALWAYS"
    )
    # the part number inside the file name is a searchable word
    assert [h.name for h in di.search(pg, "6082A12")] == ["6082A12_Draw Latch.SLDPRT"]


def test_lid_latch_finds_a_cad_file_named_only_for_latch(pg, fake):
    add_cad(fake, "latchCad", "6082A12_Draw Latch.SLDPRT")
    di.full_sync(pg, fake.google())

    names = [h.name for h in di.search(pg, "lid latch", limit=20)]

    assert "6082A12_Draw Latch.SLDPRT" in names
    # a file whose name has both words outranks one that has only "latch"
    assert names.index("Lid latch design notes") < names.index(
        "6082A12_Draw Latch.SLDPRT"
    )


def test_type_filter_on_postgres(pg, fake):
    add_cad(fake, "latchCad", "6082A12_Draw Latch.SLDPRT")
    di.full_sync(pg, fake.google())

    assert [h.name for h in di.search(pg, "latch", kind="cad")] == [
        "6082A12_Draw Latch.SLDPRT"
    ]
    assert {h.kind for h in di.search(pg, "latch", kind="doc")} == {"doc"}
    assert {h.kind for h in di.search(pg, "latch", kind="pdf")} == {"pdf"}


def test_copies_rank_below_the_original_and_identical_text_collapses(pg, fake):
    for file_id, name, day in (
        ("orig", "Seal notes", "2026-01-01"),
        ("copy", "Copy of Seal notes", "2026-09-01"),
    ):
        fake.add(file_id, name, DOC, "fDocs", modified=f"{day}T00:00:00Z")
        fake.documents[file_id] = {
            "tabs": [doc_tab("T", doc_paragraph("o-ring groove depth notes"))]
        }
    for i in range(3):
        add_cad(
            fake,
            f"dup{i}",
            "Gasket_Draw.SLDPRT",
            modified=f"2026-0{i + 1}-01T00:00:00Z",
        )
    di.full_sync(pg, fake.google())

    hits = di.search(pg, "groove depth")
    assert [h.drive_id for h in hits if h.text] == ["orig"] and hits[0].also_in == 1

    cad = di.search(pg, "gasket draw", kind="cad")
    assert len(cad) == 1 and cad[0].also_in == 2
    assert cad[0].modified_time.month == 3  # the newest of the three copies


def test_any_word_matches_but_every_word_ranks_higher(pg, fake):
    fake.add("both", "Torque sheet", DOC, "fDocs")
    fake.documents["both"] = {
        "tabs": [doc_tab("T", doc_paragraph("gasket torque values"))]
    }
    fake.add("one", "Other sheet", DOC, "fDocs")
    fake.documents["one"] = {
        "tabs": [doc_tab("T", doc_paragraph("gasket material only"))]
    }
    di.full_sync(pg, fake.google())

    ids = [
        h.drive_id
        for h in di.search(pg, "gasket torque")
        if h.drive_id in ("both", "one")
    ]

    assert ids == ["both", "one"]


def test_old_sources_rank_below_current_ones_on_postgres(pg, fake):
    season_docs(fake)
    di.full_sync(pg, fake.google())

    hits = di.search(
        pg, "gasket groove", today=NOW, demote=drive_rank.load_rules(environ={})
    )

    ids = [
        h.drive_id for h in hits if h.drive_id in ("cur", "alfie", "oldf", "oldseason")
    ]
    assert ids[0] == "cur" and set(ids) == {"cur", "alfie", "oldf", "oldseason"}
    assert {h.drive_id: h.possibly_old for h in hits}["oldseason"] is True
