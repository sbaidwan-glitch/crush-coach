"""Migrations run on throwaway SQLite files, never on the app's shared engine."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
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


def test_downgrade_to_0002_removes_only_the_tracker_columns(fresh_engine):
    db.run_migrations(fresh_engine)
    command.downgrade(db.alembic_config(fresh_engine), "0002")
    inspector = inspect(fresh_engine)
    task_columns = {c["name"] for c in inspector.get_columns("task")}
    assert task_columns.isdisjoint(TRACKER_COLUMNS)
    assert {"code", "title", "owner"} <= task_columns
    assert "coach_message" in inspector.get_table_names()


def test_downgrade_to_0001_removes_only_the_coach_tables(fresh_engine):
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


def test_downgrade_to_0005_keeps_the_drive_tables_but_drops_their_new_columns(
    fresh_engine,
):
    db.run_migrations(fresh_engine)
    command.downgrade(db.alembic_config(fresh_engine), "0005")
    inspector = inspect(fresh_engine)
    assert "search_vector" not in {
        c["name"] for c in inspector.get_columns("drive_file")
    }
    assert "heartbeat_at" not in {
        c["name"] for c in inspector.get_columns("sync_state")
    }
    assert "drive_chunk" in inspector.get_table_names()


def test_upgrade_creates_the_task_event_table(fresh_engine):
    db.run_migrations(fresh_engine)

    inspector = inspect(fresh_engine)
    columns = {c["name"]: c for c in inspector.get_columns("task_event")}
    assert set(columns) == {
        "id",
        "task_id",
        "actor",
        "from_status",
        "to_status",
        "note",
        "created_at",
    }
    assert columns["from_status"]["nullable"] is True
    assert columns["note"]["nullable"] is True
    assert columns["actor"]["nullable"] is False
    assert [fk["referred_table"] for fk in inspector.get_foreign_keys("task_event")] == [
        "task"
    ]


def test_0007_maps_older_status_words_to_the_new_flow(fresh_engine):
    config = db.alembic_config(fresh_engine)
    command.upgrade(config, "0006")
    words = {
        "A": "todo",
        "B": "in_progress",
        "C": "done",
        "D": "In Review",
        "E": "complete",
        "F": " Not Started ",
        "G": "blocked",
        "H": "something odd",
    }
    with fresh_engine.begin() as conn:
        for code, status in words.items():
            conn.execute(
                text(
                    "INSERT INTO task (code, title, subsystem, task_type, stage, status)"
                    " VALUES (:code, 't', 'Enclosure', 'Design', 'x', :status)"
                ),
                {"code": code, "status": status},
            )

    command.upgrade(config, "0007")

    with fresh_engine.connect() as conn:
        mapped = dict(conn.execute(text("SELECT code, status FROM task")).all())
    assert mapped == {
        "A": "todo",
        "B": "in_progress",
        "C": "done",
        "D": "needs_review",
        "E": "done",
        "F": "todo",
        "G": "in_progress",
        "H": "todo",  # a word the flow doesn't know starts over
    }


def test_downgrade_from_0007_drops_the_events_and_puts_review_tasks_back_in_progress(
    fresh_engine,
):
    db.run_migrations(fresh_engine)
    with fresh_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO task (code, title, subsystem, task_type, stage, status)"
                " VALUES ('R', 't', 'Enclosure', 'Design', 'x', 'needs_review')"
            )
        )

    command.downgrade(db.alembic_config(fresh_engine), "0006")

    assert "task_event" not in inspect(fresh_engine).get_table_names()
    with fresh_engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM task")).scalar() == "in_progress"


def test_downgrade_to_0004_removes_only_the_drive_index(fresh_engine):
    db.run_migrations(fresh_engine)
    command.downgrade(db.alembic_config(fresh_engine), "0004")
    tables = inspect(fresh_engine).get_table_names()
    assert {"drive_file", "drive_chunk", "sync_state"}.isdisjoint(tables)
    assert {"task", "coach_message", "llm_call"} <= set(tables)
    assert {"decision", "decision_task"} <= set(tables)


def test_upgrade_creates_decision_tables(fresh_engine):
    db.run_migrations(fresh_engine)

    inspector = inspect(fresh_engine)
    columns = {c["name"]: c for c in inspector.get_columns("decision")}
    assert set(columns) == {
        "id",
        "subsystem",
        "topic",
        "decision",
        "rationale",
        "alternatives",
        "sources",
        "status",
        "superseded_by_id",
        "proposed_by",
        "created_at",
        "confirmed_by",
        "confirmed_at",
        "drafted_by_coach",
    }
    for name in ("subsystem", "topic", "decision", "rationale", "status"):
        assert columns[name]["nullable"] is False
    for name in ("confirmed_by", "confirmed_at", "superseded_by_id"):
        assert columns[name]["nullable"] is True
    # A decision can point at the decision that replaced it.
    decision_fks = inspector.get_foreign_keys("decision")
    assert [fk["referred_table"] for fk in decision_fks] == ["decision"]

    links = inspector.get_columns("decision_task")
    assert {c["name"] for c in links} == {"decision_id", "task_id"}
    assert set(inspector.get_pk_constraint("decision_task")["constrained_columns"]) == {
        "decision_id",
        "task_id",
    }
    assert sorted(
        fk["referred_table"] for fk in inspector.get_foreign_keys("decision_task")
    ) == ["decision", "task"]


def test_downgrade_to_0003_removes_only_the_decision_tables(fresh_engine):
    db.run_migrations(fresh_engine)
    command.downgrade(db.alembic_config(fresh_engine), "0003")

    tables = inspect(fresh_engine).get_table_names()
    assert "decision" not in tables and "decision_task" not in tables
    assert {"task", "coach_message", "llm_call"} <= set(tables)


def test_upgrade_creates_team_facts_with_the_three_starting_facts(fresh_engine):
    db.run_migrations(fresh_engine)

    inspector = inspect(fresh_engine)
    columns = {c["name"]: c for c in inspector.get_columns("team_fact")}
    assert set(columns) == {"id", "text", "updated_by", "updated_at"}
    assert columns["text"]["nullable"] is False
    with fresh_engine.connect() as connection:
        rows = connection.execute(text("SELECT text FROM team_fact ORDER BY id")).all()
    assert [r[0] for r in rows] == [
        "Current season is 2026-27",
        "Crush is the current vehicle; Alfie docs are older and may be stale",
        "Current Crush CAD lives in the 2025-2026 Mechanical CAD folder",
    ]


def test_downgrade_to_0006_removes_only_the_team_facts(fresh_engine):
    db.run_migrations(fresh_engine)
    command.downgrade(db.alembic_config(fresh_engine), "0006")

    tables = inspect(fresh_engine).get_table_names()
    assert "team_fact" not in tables
    assert {"task", "drive_file", "decision"} <= set(tables)


def test_migrations_match_models(fresh_engine):
    """Fails when a model changed without a new migration."""
    db.run_migrations(fresh_engine)
    with fresh_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, SQLModel.metadata)
    assert diff == [], f"models changed without an Alembic migration: {diff}"
