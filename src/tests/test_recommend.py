"""The recommendation tools: search the reference books, calculate. Made-up files."""

import json
from contextlib import nullcontext

import pytest
from index_helpers import add_file

import recommend
import retrieval

HANDBOOK_LINK = "https://drive.google.com/file/d/1HandbookHandbookHandbook/view"
NOTES_LINK = "https://docs.google.com/document/d/1ReferenceNotesNotesNotes/edit"


@pytest.fixture
def found():
    return retrieval.Retrieval(
        facts=[], decisions=[], decisions_failed=False, chunks=[], cad=[], earlier=[]
    )


@pytest.fixture
def kit(session, found):
    add_file(
        session,
        "Parker O-Ring Handbook.pdf",
        kind="pdf",
        folder="MuddSub / Library / Textbooks",
        modified="2020-06-01",
        chunks=[
            (
                "Static face seal gland design: squeeze should be 15 to 30 percent of "
                "the cross section for flat gaskets and o-rings."
            )
        ],
        drive_id="handbook0001",
        link=HANDBOOK_LINK,
    )
    add_file(
        session,
        "Gasket design notes",
        folder="MuddSub / Mechanical / 25-26 References",
        chunks=["Gasket squeeze notes: the team used 20 percent squeeze."],
        drive_id="refNotes0001",
        link=NOTES_LINK,
    )
    add_file(
        session,
        "Random meeting notes",
        folder="MuddSub / Meetings",
        chunks=["Gasket squeeze was mentioned once in passing."],
        drive_id="meeting00001",
    )
    return recommend.RecommendKit(found, lambda: nullcontext(session))


def search(kit, query="gasket squeeze"):
    return json.loads(kit.search_references({"query": query}, 10.0))


def test_the_books_and_the_reference_folder_are_searched_and_other_files_are_not(kit):
    titles = [r["title"] for r in search(kit)["results"]]

    assert "Parker O-Ring Handbook.pdf" in titles
    assert "Gasket design notes" in titles  # in a "References" folder
    assert "Random meeting notes" not in titles  # a doc outside the references


def test_each_result_has_a_number_a_link_a_place_a_date_and_the_passage(kit):
    [book] = [r for r in search(kit)["results"] if r["kind"] == "pdf"]

    assert book["ref"].startswith("R")
    assert book["link"] == HANDBOOK_LINK
    assert book["folder"] == "MuddSub / Library / Textbooks"
    assert book["modified"] == "2020-06-01"
    assert "15 to 30 percent" in book["passage"]


def test_found_links_become_citable_so_the_answer_check_keeps_them(kit, found):
    search(kit)

    assert HANDBOOK_LINK in found.known and NOTES_LINK in found.known
    assert found.known[HANDBOOK_LINK]["title"] == "Parker O-Ring Handbook.pdf"


def test_the_same_passage_keeps_its_number_across_searches(kit):
    first = {r["title"]: r["ref"] for r in search(kit)["results"]}
    again = {
        r["title"]: r["ref"] for r in search(kit, "squeeze cross section")["results"]
    }

    assert again["Parker O-Ring Handbook.pdf"] == first["Parker O-Ring Handbook.pdf"]
    assert set(kit.refs) == set(first.values())


def test_numbers_resolve_to_the_passages(kit):
    refs = {r["ref"]: r["title"] for r in search(kit)["results"]}

    assert {ref: hit.name for ref, hit in kit.refs.items()} == refs


def test_nothing_found_says_so_and_invents_nothing(kit):
    result = search(kit, "quantum flux capacitor")

    assert result["results"] == [] and "Nothing" in result["note"]
    assert kit.refs == {}


def test_an_empty_query_is_an_error_message_not_a_crash(kit):
    assert "error" in json.loads(kit.search_references({"query": "  "}, 10.0))
    assert "error" in json.loads(kit.search_references({}, 10.0))


def test_calculate_returns_the_exact_result(kit):
    result = json.loads(
        kit.calculate({"expression": "0.030 / 0.050", "what": "clearance ratio"}, 10.0)
    )

    assert result == {
        "what": "clearance ratio",
        "expression": "0.030 / 0.050",
        "result": "0.6",
    }


def test_calculate_refuses_anything_but_arithmetic_with_a_message_the_model_can_read(
    kit,
):
    result = json.loads(kit.calculate({"expression": "__import__('os')"}, 10.0))

    assert "error" in result and "result" not in result


def test_the_two_tools_are_offered_with_steps_the_page_can_show(kit):
    tools = {t.name: t for t in kit.tools()}

    assert set(tools) == {"search_references", "calculate"}
    assert tools["search_references"].step.startswith("Searching")
    assert tools["calculate"].spec["function"]["name"] == "calculate"


def test_a_textbook_in_an_old_seasons_folder_is_not_called_old_but_an_alfie_one_is(
    session, found
):
    import contextlib

    import drive_index
    import drive_rank

    old_book = add_file(
        session,
        "Machinery Handbook.pdf",
        kind="pdf",
        folder="MuddSub / 2020-2021 / Library",
        chunks=["Handbook says clearance for gaskets depends on stock thickness."],
        drive_id="oldbook00001",
    )
    add_file(
        session,
        "Alfie Notes.pdf",
        kind="pdf",
        folder="MuddSub / OBSOLETE / Alfie",
        chunks=["Alfie notes say gasket clearance stock thickness varies."],
        drive_id="alfienotes01",
    )
    rules = drive_rank.load_rules()
    original = drive_index.demote_rules
    drive_index.demote_rules = lambda: rules
    try:
        kit = recommend.RecommendKit(found, lambda: contextlib.nullcontext(session))
        results = {
            r["title"]: r
            for r in json.loads(
                kit.search_references({"query": "gasket clearance stock thickness"}, 5)
            )["results"]
        }
    finally:
        drive_index.demote_rules = original

    assert old_book.name in results and results[old_book.name]["possibly_old"] is None
    assert "Alfie" in (results["Alfie Notes.pdf"]["possibly_old"] or "")
