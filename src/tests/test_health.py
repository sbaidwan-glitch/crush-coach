from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlmodel import Session, select

import db
import main
from models import Task


def test_health():
    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_is_accessible_html():
    with TestClient(main.app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "<title>Crush Coach</title>" in html
    assert '<html lang="en">' in html
    assert 'name="viewport"' in html
    assert '<main class="main" id="main">' in html
    assert 'href="#main"' in html  # skip link
    assert html.count("<h1 ") == 1
    assert '<nav class="sidebar" aria-label="Main">' in html


def test_stylesheets_and_fonts_are_served_and_the_old_one_is_not():
    with TestClient(main.app) as client:
        for name in ("tokens", "app", "extras"):
            response = client.get(f"/static/styles/{name}.css")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/css")
        font = client.get("/static/fonts/manrope/manrope-latin-400-normal.woff2")
        assert font.status_code == 200
        assert client.get("/static/img/crush.png").status_code == 200
        assert client.get("/static/style.css").status_code == 404


def test_migrations_run_on_startup_and_task_round_trips():
    with TestClient(main.app):
        assert "task" in inspect(db.engine).get_table_names()
    with Session(db.engine) as session:
        session.add(
            Task(
                code="X01",
                title="Example task",
                subsystem="Enclosure",
                task_type="Fabricate",
                stage="drawing",
            )
        )
        session.commit()
        task = session.exec(select(Task).where(Task.code == "X01")).one()
    assert task.id is not None
    assert task.status == "todo"
    assert task.owner is None


def test_pages_still_work_when_migrations_fail(monkeypatch, caplog):
    def boom():
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(db, "run_migrations", boom)
    with caplog.at_level("ERROR", logger="crush_coach"), TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200
    assert "Could not run database migrations (RuntimeError)" in caplog.text
    assert "secret" not in caplog.text
