"""Parsing the tracker CSV. Fixtures are made-up; never use a real export here."""

from pathlib import Path

import pytest

import import_tasks as imp

FIXTURES = Path(__file__).parent / "fixtures"
REAL = (FIXTURES / "tracker_real_layout.csv").read_text(encoding="utf-8")
ACTUATORS = (FIXTURES / "tracker_actuators_layout.csv").read_text(encoding="utf-8")


def by_code(parsed: imp.ParsedFile) -> dict[str, imp.ParsedRow]:
    return {r.code: r for r in parsed.rows if r.code}


def test_real_layout_skips_title_rows_and_finds_the_hash_header():
    parsed = imp.parse_tracker_csv(REAL)

    assert parsed.format == imp.FORMAT_TRACKER
    codes = [r.code for r in parsed.rows]
    assert codes == ["M01", "M02", "M03", "M04", "M05", "M22(2)", "M22(2)", "", "M07", "E01"]
    assert not any("MADE-UP" in r.title or "Notes" in r.title for r in parsed.rows)


def test_real_layout_maps_every_column_including_the_misspelled_one():
    row = by_code(imp.parse_tracker_csv(REAL))["M01"]

    assert row.title == "Cut example wall panels"
    assert row.owner == "Sam Example"
    assert row.details["description"] == "Cut two example panels.\nDeburr the edges."
    assert row.details["dependencies"] == "M05"
    assert row.details["exploratory_questions"] == "Which sheet stock?"
    assert row.details["skills"] == "Waterjet basics"
    assert row.details["requirements"] == "Panels flat to the example spec"
    assert row.details["people_to_ask"] == "Riley Sample"
    assert row.details["documentation"] == "https://docs.google.com/document/d/example/edit"
    assert row.details["due_date"] == "Week 3"
    assert row.error is None


def test_blank_cells_become_none_and_unknown_columns_are_ignored():
    row = by_code(imp.parse_tracker_csv(REAL))["E01"]

    assert row.owner is None
    assert row.details["description"] is None
    assert row.details["due_date"] == "TBD"


def test_correctly_spelled_documentation_header_also_works():
    text = "#,Task,Relevant Documentation\nM01,Cut example panel,see the example doc\n"

    row = imp.parse_tracker_csv(text).rows[0]

    assert row.details["documentation"] == "see the example doc"


def test_blank_code_blank_title_and_duplicate_code_are_row_errors():
    rows = imp.parse_tracker_csv(REAL).rows
    errors = {(r.line, r.code): r.error for r in rows if r.error}

    assert len(errors) == 3
    duplicate = next(r for r in rows if r.code == "M22(2)" and r.error)
    assert "duplicate code M22(2)" in duplicate.error
    first = next(r for r in rows if r.code == "M22(2)" and not r.error)
    assert first.line < duplicate.line
    blank_code = next(r for r in rows if r.code == "" and r.error)
    assert blank_code.error == "code is empty"
    blank_title = next(r for r in rows if r.code == "M07")
    assert blank_title.error == "title is empty"


def test_row_lines_point_at_the_file_even_with_multiline_cells():
    rows = by_code(imp.parse_tracker_csv(REAL))

    assert rows["M01"].line == 6  # the header cell above spans two lines
    assert rows["M02"].line == 8  # M01's description spans two lines


def test_mission_actuators_layout_guesses_from_title_then_code_letter():
    rows = by_code(imp.parse_tracker_csv(ACTUATORS))

    assert rows["A01"].subsystem is None  # "A" is ambiguous, so the lead chooses
    assert rows["G01"].subsystem == "Gripper"
    assert rows["T01"].subsystem == "Torpedo"
    assert rows["A01"].task_type == "Design"  # "Select"
    assert rows["T01"].task_type == "Design"  # "Model"


@pytest.mark.parametrize(
    ("title", "code", "expected"),
    [
        ("Cut example wall panels", "M01", "Enclosure"),
        ("Cut gasket for the lid", "M02", "Seals/Waterproofing"),
        ("Machine thruster bracket", "M03", "Thrusters"),
        ("Make claw jaw", "M04", "Gripper"),
        ("Print board standoffs", "M05", "Electronics Mounts"),
        ("Torpedo tube", "M06", "Torpedo"),
        ("Mill example widget", "M07", None),
        ("Mill example widget", "E09", "Electronics Mounts"),
        ("Mill example widget", "", None),
    ],
)
def test_guess_subsystem(title, code, expected):
    assert imp.guess_subsystem(title, code) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Test lid fit", "Validate"),
        ("Verify the seal", "Validate"),
        ("Design a latch", "Design"),
        ("CAD the tray", "Design"),
        ("Cut panels", "Fabricate"),
    ],
)
def test_guess_task_type(title, expected):
    assert imp.guess_task_type(title) == expected


def test_sample_layout_is_still_accepted_with_explicit_columns():
    text = (
        "code,title,subsystem,task_type,stage,owner,status\n"
        "X01,Example,Gripper,Design,requirements,Sam Example,in_progress\n"
        "X02,Bad,Cockpit,Design,requirements,,\n"
    )

    parsed = imp.parse_tracker_csv(text)

    assert parsed.format == imp.FORMAT_SAMPLE
    good, bad = parsed.rows
    assert (good.subsystem, good.task_type, good.stage, good.status) == (
        "Gripper",
        "Design",
        "requirements",
        "in_progress",
    )
    assert good.owner == "Sam Example"
    assert bad.error == "unknown subsystem 'Cockpit'"


def test_over_long_field_is_a_row_error():
    text = f"#,Task,Detailed Description\nM01,Example,{'x' * (imp.MAX_FIELD_CHARS + 1)}\n"

    assert "description is longer" in imp.parse_tracker_csv(text).rows[0].error


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "empty"),
        ("   \n\n", "empty"),
        ("just,some,columns\n1,2,3\n", "header row"),
        ("code,title\nX01,Example\n", "missing column"),
        ("#,Owner\nM01,Sam\n", "missing column"),
    ],
)
def test_unusable_files_raise_a_plain_error(text, message):
    with pytest.raises(imp.ImportFileError, match=message):
        imp.parse_tracker_csv(text)


def test_decode_rejects_bad_bytes_empty_and_oversize():
    with pytest.raises(imp.ImportFileError, match="UTF-8"):
        imp.decode_csv_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(imp.ImportFileError, match="empty"):
        imp.decode_csv_bytes(b"  \n")
    with pytest.raises(imp.ImportFileError, match="too large"):
        imp.decode_csv_bytes(b"x" * (imp.MAX_BYTES + 1))


def test_decode_strips_a_byte_order_mark():
    text = imp.decode_csv_bytes(b"\xef\xbb\xbf#,Task\nM01,Example\n")
    assert imp.parse_tracker_csv(text).rows[0].code == "M01"
