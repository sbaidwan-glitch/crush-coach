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


def test_blank_cells_become_none_and_unknown_columns_are_ignored():
    row = by_code(imp.parse_tracker_csv(REAL))["E01"]

    assert row.owner is None
    assert row.details["description"] is None
    assert row.details["due_date"] == "TBD"


def test_correctly_spelled_documentation_header_also_works():
    text = "#,Task,Relevant Documentation\nM01,Cut example panel,see the example doc\n"

    row = imp.parse_tracker_csv(text).rows[0]

    assert row.details["documentation"] == "see the example doc"


def test_parsing_does_not_judge_rows_so_the_lead_can_fix_them_in_place():
    rows = imp.parse_tracker_csv(REAL).rows
    blank_code = next(r for r in rows if r.code == "" and r.title)
    blank_title = next(r for r in rows if r.code == "M07")

    assert blank_code.title.startswith("Orphan")
    assert blank_title.title == ""


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
    assert bad.subsystem == "Cockpit"  # kept as typed; check_rows asks for a fix


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


# --- checking rows (what the review page shows) ---


def row(code="M30", title="Example", subsystem="Gripper", task_type="Design", **kw):
    return imp.ParsedRow(
        line=kw.pop("line", 2),
        code=code,
        title=title,
        subsystem=subsystem,
        task_type=task_type,
        **kw,
    )


def test_valid_rows_are_added_and_existing_codes_are_skipped():
    checks = imp.check_rows([row("M30"), row("M31")], existing={"M31"})

    assert [c.state for c in checks] == ["add", "exists"]
    assert all(not c.problems for c in checks)


def test_duplicate_code_asks_for_a_new_one_and_offers_the_next_free_code():
    rows = [row("M22(2)"), row("M22(2)"), row("M24")]

    first, second, third = imp.check_rows(rows, existing={"M21"})

    assert (first.state, third.state) == ("add", "add")
    assert second.state == "fix"
    problem = second.problem("code")
    assert problem.message == "Two rows use M22(2). Give this one a new code:"
    assert problem.suggestion == "M25"


def test_two_problem_rows_are_never_offered_the_same_code():
    checks = imp.check_rows([row("M01"), row("M01"), row("M01")], existing=set())

    assert [c.problem("code").suggestion for c in checks[1:]] == ["M02", "M03"]


def test_fixing_the_first_row_clears_the_duplicate_on_the_second():
    rows = [row("M22(3)"), row("M22(2)")]  # the lead renamed the first one

    assert [c.state for c in imp.check_rows(rows, existing=set())] == ["add", "add"]


def test_leaving_a_row_out_removes_its_problems_and_its_claim_on_the_code():
    rows = [row("M01", subsystem=None), row("M01")]

    checks = imp.check_rows(rows, existing=set(), included=[False, True])

    assert [c.state for c in checks] == ["left_out", "add"]


def test_blank_code_borrows_the_most_common_letters():
    checks = imp.check_rows([row("M01"), row("M02"), row("E01"), row("")], set())

    assert checks[3].problem("code").message == "Add a code for this row."
    assert checks[3].problem("code").suggestion == "M03"


def test_a_typed_code_that_already_exists_is_a_problem_but_an_unedited_one_is_not():
    rows = [row("M05"), row("M09")]

    checks = imp.check_rows(rows, {"M05", "M09"}, original_codes=["M05", "M22(2)"])

    assert checks[0].state == "exists"
    assert checks[1].state == "fix"
    assert checks[1].problem("code").message.startswith("M09 is already in the tracker")


def test_other_problems_use_short_plain_messages_per_field():
    bad = row("M01", title="", subsystem=None, task_type="Welding", status="finished")

    (check,) = imp.check_rows([bad], set())

    assert check.problem("title").message == "Add a title."
    assert check.problem("subsystem").message == "Choose a subsystem."
    assert check.problem("task_type").message == "Choose a type."
    assert "Status “finished” isn’t valid" in check.problem("row").message
    (cockpit,) = imp.check_rows([row(subsystem="Cockpit")], set())
    assert cockpit.problem("subsystem").message == "“Cockpit” isn’t a subsystem. Choose one."


def test_over_long_text_is_a_row_problem_that_cannot_be_fixed_in_place():
    details = {**dict.fromkeys(imp.DETAIL_FIELDS), "description": "x" * 5001}

    (check,) = imp.check_rows([row(details=details)], set())

    assert "Description is over 5,000 characters" in check.problem("row").message


@pytest.mark.parametrize(
    ("code", "taken", "expected"),
    [
        ("M22(2)", {"M21", "M24"}, "M25"),
        ("M9", {"M9"}, "M10"),
        ("M05", {"M05"}, "M06"),
        ("", {"E01", "M01", "M02"}, "M03"),
        ("Enclosure", {"M01"}, "M02"),
        ("", set(), None),
    ],
)
def test_next_free_code(code, taken, expected):
    assert imp.next_free_code(code, taken) == expected


def test_a_row_whose_code_already_exists_is_skipped_even_with_other_problems():
    (check,) = imp.check_rows([row("M05", subsystem=None, title="")], {"M05"})

    assert check.state == "exists"
