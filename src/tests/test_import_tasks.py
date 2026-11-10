from pathlib import Path

import pytest
from sqlmodel import select

import db
import import_tasks
from models import SUBSYSTEMS, Task

SAMPLE = Path(__file__).parent.parent / "sample_data" / "tasks.csv"
HEADER = "code,title,subsystem,task_type,stage,owner,status\n"


def write_csv(tmp_path, body: str, header: str = HEADER) -> Path:
    path = tmp_path / "tasks.csv"
    path.write_text(header + body, encoding="utf-8")
    return path


def test_sample_csv_imports_ten_made_up_tasks(session):
    result = import_tasks.import_tasks(session, SAMPLE)

    assert (result.added, result.skipped_existing, result.errors) == (10, 0, [])
    tasks = session.exec(select(Task)).all()
    assert len(tasks) == 10
    assert {t.subsystem for t in tasks} == set(SUBSYSTEMS)
    assert {t.status for t in tasks} == {"todo", "in_progress", "done"}
    assert {t.task_type for t in tasks} == {"Fabricate", "Design", "Validate"}


def test_importing_twice_adds_nothing(session):
    import_tasks.import_tasks(session, SAMPLE)
    second = import_tasks.import_tasks(session, SAMPLE)

    assert (second.added, second.skipped_existing) == (0, 10)
    assert len(session.exec(select(Task)).all()) == 10


def test_existing_task_is_not_overwritten(session):
    session.add(
        Task(
            code="M02",
            title="Edited by a lead",
            subsystem="Enclosure",
            task_type="Fabricate",
            stage="inspect",
            status="done",
        )
    )
    session.commit()

    import_tasks.import_tasks(session, SAMPLE)

    kept = session.exec(select(Task).where(Task.code == "M02")).one()
    assert kept.title == "Edited by a lead"
    assert kept.status == "done"


def test_blank_owner_and_status_default(session, tmp_path):
    path = write_csv(tmp_path, "X01,Example,Gripper,Design,requirements,,\n")

    import_tasks.import_tasks(session, path)

    task = session.exec(select(Task)).one()
    assert task.owner is None
    assert task.status == "todo"


def test_bad_rows_are_reported_and_skipped_but_good_rows_import(session, tmp_path):
    path = write_csv(
        tmp_path,
        "X01,Good one,Gripper,Design,requirements,,todo\n"
        ",No code,Gripper,Design,requirements,,todo\n"
        "X03,Bad subsystem,Cockpit,Design,requirements,,todo\n"
        "X04,Bad type,Gripper,Welding,requirements,,todo\n"
        "X05,Bad status,Gripper,Design,requirements,,finished\n"
        "X01,Duplicate code,Gripper,Design,requirements,,todo\n"
        "X07,Good two,Torpedo,Validate,test plan,,done\n",
    )

    result = import_tasks.import_tasks(session, path)

    assert result.added == 2
    assert len(result.errors) == 5
    assert result.errors[0] == "line 3: Add a code for this row."
    assert "“Cockpit” isn’t a subsystem" in result.errors[1]
    assert result.errors[2].startswith("line 5: Choose a type.")
    assert "Status “finished” isn’t valid" in result.errors[3]
    assert result.errors[4] == "line 7: Two rows use X01. Give this one a new code"
    codes = {t.code for t in session.exec(select(Task)).all()}
    assert codes == {"X01", "X07"}


def test_missing_column_is_a_plain_error(session, tmp_path):
    path = write_csv(tmp_path, "X01,Example\n", header="code,title\n")
    with pytest.raises(import_tasks.ImportFileError, match="missing column"):
        import_tasks.import_tasks(session, path)


def test_missing_file_is_a_plain_error(session, tmp_path):
    with pytest.raises(import_tasks.ImportFileError, match="Could not read"):
        import_tasks.import_tasks(session, tmp_path / "nope.csv")


def test_cli_reports_counts_and_exits_zero(session, capsys):
    code = import_tasks.main([str(SAMPLE)])
    out = capsys.readouterr()
    assert code == 0
    assert "10 added, 0 already present, 0 rejected." in out.out

    code = import_tasks.main([str(SAMPLE)])
    assert code == 0
    assert "0 added, 10 already present, 0 rejected." in capsys.readouterr().out


def test_cli_exits_two_when_rows_are_rejected(session, tmp_path, capsys):
    path = write_csv(
        tmp_path,
        "X01,Good,Gripper,Design,requirements,,todo\nX02,,Gripper,Design,x,,\n",
    )
    code = import_tasks.main([str(path)])
    out = capsys.readouterr()
    assert code == 2
    assert "1 added" in out.out
    assert "rejected line 3" in out.err


def test_cli_exits_one_for_a_missing_file(session, tmp_path, capsys):
    code = import_tasks.main([str(tmp_path / "nope.csv")])
    assert code == 1
    assert "Import failed: Could not read" in capsys.readouterr().err


def test_cli_database_failure_does_not_print_the_url(session, monkeypatch, capsys):
    def boom():
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(db, "run_migrations", boom)

    code = import_tasks.main([str(SAMPLE)])

    err = capsys.readouterr().err
    assert code == 1
    assert "database error (RuntimeError)" in err
    assert "secret" not in err
