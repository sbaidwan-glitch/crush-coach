"""Migrations run on throwaway SQLite files, never on the app's shared engine."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlmodel import SQLModel

import db

TRACKER_COLUMNS = (
    "description",
    "dependencies",
    "exploratory_questions",
    "skills",
    "requirements",
    "people_to_ask",
    "documentation",
    "due_date",
)


@pytest.fixture
def fresh_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migrations.db'}")
    yield engine
    engine.dispose()


def test_upgrade_creates_task_table(fresh_engine):
    assert "task" not in inspect(fresh_engine).get_table_names()

    db.run_migrations(fresh_engine)

    inspector = inspect(fresh_engine)
    assert "task" in inspector.get_table_names()
    columns = {c["name"]: c for c in inspector.get_columns("task")}
    assert set(columns) == {
        "id",
        "code",
        "title",
        "subsystem",
        "task_type",
        "stage",
        "owner",
        "status",
        *TRACKER_COLUMNS,
    }
    assert columns["owner"]["nullable"] is True
    assert columns["code"]["nullable"] is False
    assert inspector.get_pk_constraint("task")["constrained_columns"] == ["id"]
    code_index = [
        i for i in inspector.get_indexes("task") if i["name"] == "ix_task_code"
    ]
    assert len(code_index) == 1
    assert code_index[0]["unique"]


def test_upgrade_creates_coach_tables(fresh_engine):
    db.run_migrations(fresh_engine)

    inspector = inspect(fresh_engine)
    messages = {c["name"] for c in inspector.get_columns("coach_message")}
    assert messages == {
        "id",
        "task_id",
        "member",
        "role",
        "content",
        "meta",
        "created_at",
    }
    calls = {c["name"]: c for c in inspector.get_columns("llm_call")}
    assert set(calls) == {
        "id",
        "task_id",
        "member",
        "model",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "cost",
        "status",
        "created_at",
    }
    assert calls["cost"]["nullable"] is True  # only when the response provides it
    assert calls["latency_ms"]["nullable"] is False
    for table in ("coach_message", "llm_call"):
        foreign_keys = inspector.get_foreign_keys(table)
        assert [fk["referred_table"] for fk in foreign_keys] == ["task"]


def test_upgrade_is_repeatable(fresh_engine):
    db.run_migrations(fresh_engine)
    db.run_migrations(fresh_engine)  # already at head: must be a no-op, not an error
    assert "task" in inspect(fresh_engine).get_table_names()


def test_downgrade_removes_task_table(fresh_engine):
    db.run_migrations(fresh_engine)
    command.downgrade(db.alembic_config(fresh_engine), "base")
    assert "task" not in inspect(fresh_engine).get_table_names()


def test_tracker_columns_are_nullable_text(fresh_engine):
    db.run_migrations(fresh_engine)
    columns = {c["name"]: c for c in inspect(fresh_engine).get_columns("task")}
    for name in TRACKER_COLUMNS:
        assert columns[name]["nullable"] is True


def test_downgrade_one_step_removes_only_the_tracker_columns(fresh_engine):
    db.run_migrations(fresh_engine)
    command.downgrade(db.alembic_config(fresh_engine), "0002")
    inspector = inspect(fresh_engine)
    task_columns = {c["name"] for c in inspector.get_columns("task")}
    assert task_columns.isdisjoint(TRACKER_COLUMNS)
    assert {"code", "title", "owner"} <= task_columns
    assert "coach_message" in inspector.get_table_names()


def test_downgrade_two_steps_removes_only_the_coach_tables(fresh_engine):
    db.run_migrations(fresh_engine)
    command.downgrade(db.alembic_config(fresh_engine), "0001")
    tables = inspect(fresh_engine).get_table_names()
    assert "task" in tables
    assert "coach_message" not in tables and "llm_call" not in tables


def test_upgrade_creates_drive_index_tables(fresh_engine):
    db.run_migrations(fresh_engine)

    inspector = inspect(fresh_engine)
    files = {c["name"]: c for c in inspector.get_columns("drive_file")}
    assert set(files) == {
        "id",
        "drive_id",
        "name",
        "mime_type",
        "parent_id",
        "folder_path",
        "modified_time",
        "web_link",
        "size",
        "kind",
        "text_status",
        "text_reason",
        "last_indexed_at",
        "search_vector",
    }
    drive_id_index = [
        i
        for i in inspector.get_indexes("drive_file")
        if i["column_names"] == ["drive_id"]
    ]
    assert len(drive_id_index) == 1 and drive_id_index[0]["unique"]

    chunks = {c["name"] for c in inspector.get_columns("drive_chunk")}
    assert chunks == {"id", "file_id", "position", "heading", "text", "search_vector"}
    assert [
        fk["referred_table"] for fk in inspector.get_foreign_keys("drive_chunk")
    ] == ["drive_file"]
    names = {i["name"] for i in inspector.get_indexes("drive_chunk")}
    assert "ix_drive_chunk_search_vector" in names

    state = {c["name"] for c in inspector.get_columns("sync_state")}
    assert {
        "page_token",
        "last_full_sync",
        "last_incremental_sync",
        "heartbeat_at",
    } <= state
    file_indexes = {i["name"] for i in inspector.get_indexes("drive_file")}
    assert "ix_drive_file_search_vector" in file_indexes


def test_downgrade_one_step_keeps_the_drive_tables_but_drops_their_new_columns(
    fresh_engine,
):
    db.run_migrations(fresh_engine)
    command.downgrade(db.alembic_config(fresh_engine), "-1")
    inspector = inspect(fresh_engine)
    assert "search_vector" not in {
        c["name"] for c in inspector.get_columns("drive_file")
    }
    assert "heartbeat_at" not in {
        c["name"] for c in inspector.get_columns("sync_state")
    }
    assert "drive_chunk" in inspector.get_table_names()


def test_downgrade_to_0003_removes_only_the_drive_index(fresh_engine):
    db.run_migrations(fresh_engine)
    command.downgrade(db.alembic_config(fresh_engine), "0003")
    tables = inspect(fresh_engine).get_table_names()
    assert {"drive_file", "drive_chunk", "sync_state"}.isdisjoint(tables)
    assert {"task", "coach_message", "llm_call"} <= set(tables)


def test_migrations_match_models(fresh_engine):
    """Fails when a model changed without a new migration."""
    db.run_migrations(fresh_engine)
    with fresh_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, SQLModel.metadata)
    assert diff == [], f"models changed without an Alembic migration: {diff}"
