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

import db
import drive_index as di
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
    # every word must match, so the datasheet (no "lid") only comes up for "latch"
    assert "Latch datasheet.pdf" not in {h.name for h in hits}
    assert "Latch datasheet.pdf" in {h.name for h in di.search(pg, "latch")}
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
    for twin in ("twinOld", "twinNew"):
        fake.documents[twin] = {
            "tabs": [doc_tab("T", doc_paragraph("gasket torque plan"))]
        }
    di.full_sync(pg, fake.google())
    assert [h.drive_id for h in di.search(pg, "gasket torque")[:2]] == [
        "twinNew",
        "twinOld",
    ]


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
