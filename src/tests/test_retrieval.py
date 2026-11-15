"""What the coach is given, gathered with no model call. Made-up files and people."""

import pytest
from index_helpers import add_file
from sqlmodel import select

import retrieval
from models import Decision, DriveChunk, Task, TeamFact, utc_now


@pytest.fixture
def task(session):
    row = Task(
        code="M04",
        title="Finish enclosure wall panels",
        subsystem="Enclosure",
        task_type="Fabricate",
        stage="drawing",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def decision(session, status="confirmed", subsystem="Enclosure", topic="Latch style"):
    row = Decision(
        subsystem=subsystem,
        topic=topic,
        decision="Use the two-point cam latch.",
        rationale="It seals evenly.",
        alternatives=[{"option": "Toggle", "rejected_because": "Loosens."}],
        sources=[
            {
                "title": "Latch notes",
                "link": "https://docs.google.com/document/d/1DecisionSource00/edit",
                "year": 2026,
            }
        ],
        status=status,
        proposed_by="sam@example.edu",
        confirmed_at=utc_now() if status == "confirmed" else None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_an_empty_index_stops_before_anything_else(session, task):
    with pytest.raises(retrieval.IndexEmpty):
        retrieval.retrieve(session, task, "Which latch?")


def test_the_question_is_the_query_and_the_task_only_boosts(task):
    query, boost = retrieval.search_terms(task, "Which latch?")

    assert query == "Which latch?"
    assert boost == "Finish enclosure wall panels Enclosure"


def test_a_short_follow_up_is_searched_with_the_last_question(task):
    short, _ = retrieval.search_terms(task, "and the second one?", "Which latch?")
    long, _ = retrieval.search_terms(
        task,
        "Please tell me about the process router for the side panel",
        "Which latch?",
    )

    assert "Which latch?" in short
    assert "Which latch?" not in long


def test_a_long_task_title_cannot_outrank_the_question(session):
    """Before, the title's words were searched as hard as the question's, so a chunk
    that repeated them beat the one that answered the question."""
    long_title = Task(
        code="M99",
        title="Finish enclosure wall panels weld fillets baseplate bellypan bracket",
        subsystem="Enclosure",
        task_type="Fabricate",
        stage="drawing",
    )
    add_file(session, "Latch answer", chunks=["The draw latch is 6082A12."])
    add_file(
        session,
        "Panel chatter",
        chunks=["Finish enclosure wall panels weld fillets baseplate bellypan bracket"],
    )

    found = retrieval.retrieve(session, long_title, "Which latch?")

    assert found.chunks[0].name == "Latch answer"
    assert "Panel chatter" in [h.name for h in found.chunks]  # still a weak match


def test_the_boost_alone_finds_documents_for_a_vague_question(session, task):
    add_file(session, "Wall panel notes", chunks=["Panels are welded in two passes."])

    found = retrieval.retrieve(session, task, "How do I start?")

    assert [h.name for h in found.chunks] == ["Wall panel notes"]


def test_chunks_are_ranked_two_per_file_and_capped_at_eight(session, task):
    add_file(session, "Latch notes", chunks=[f"latch detail {i}" for i in range(5)])
    for i in range(14):
        add_file(
            session,
            f"Doc {i}",
            chunks=[f"latch mention number {i}"],
            drive_id=f"doc{i:010d}",
        )

    found = retrieval.retrieve(session, task, "latch")

    assert len(found.chunks) == retrieval.CHUNK_LIMIT == 8
    per_file = {}
    for hit in found.chunks:
        per_file[hit.file_id] = per_file.get(hit.file_id, 0) + 1
    assert max(per_file.values()) == retrieval.MAX_PER_FILE == 2


def test_cad_files_are_names_only_and_newest_first(session, task):
    add_file(session, "Latch notes", chunks=["latch"])
    add_file(session, "Enclosure_v1.SLDPRT", kind="cad", modified="2026-01-01")
    add_file(session, "Enclosure_v3.SLDPRT", kind="cad", modified="2026-10-09")
    add_file(session, "Enclosure_v2.SLDPRT", kind="cad", modified="2026-05-01")

    found = retrieval.retrieve(session, task, "Where is the enclosure file?")

    assert [h.name for h in found.cad] == [
        "Enclosure_v3.SLDPRT",
        "Enclosure_v2.SLDPRT",
        "Enclosure_v1.SLDPRT",
    ]
    assert all(h.kind != "cad" for h in found.chunks)


def test_confirmed_decisions_for_the_subsystem_come_along_and_nothing_else(
    session, task
):
    add_file(session, "Latch notes", chunks=["latch"])
    mine = decision(session)
    decision(session, status="proposed", topic="A proposal")
    decision(session, subsystem="Thrusters", topic="Thruster mount")

    found = retrieval.retrieve(session, task, "anything at all")

    assert [d["id"] for d in found.decisions] == [mine.id]
    assert f"/decisions/{mine.id}" in found.known
    assert found.known[f"/decisions/{mine.id}"]["title"].startswith("Record: ")
    # and its own Drive sources may be cited
    assert "https://docs.google.com/document/d/1DecisionSource00/edit" in found.known


def test_a_failing_decision_lookup_does_not_stop_the_answer(monkeypatch, session, task):
    add_file(session, "Latch notes", chunks=["latch"])

    def boom(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    monkeypatch.setattr(retrieval.decisions, "search_confirmed", boom)

    found = retrieval.retrieve(session, task, "latch")

    assert found.decisions_failed is True and found.chunks
    assert "could not be loaded" in retrieval.user_content(found, "latch")


def test_every_team_fact_is_in_the_prompt(session, task):
    add_file(session, "Latch notes", chunks=["latch"])
    session.add(TeamFact(text="Current season is 2026-27"))
    session.add(TeamFact(text="Crush is the current vehicle"))
    session.commit()

    found = retrieval.retrieve(session, task, "latch")
    context = retrieval.system_context({"Code": "M04"}, found.facts, "2026-11-11")

    assert found.facts == ["Current season is 2026-27", "Crush is the current vehicle"]
    assert "- Current season is 2026-27" in context
    assert "- Crush is the current vehicle" in context
    assert "2026-11-11" in context and "- Code: M04" in context


def test_the_prompt_shows_sources_with_place_date_and_age(session, task):
    add_file(
        session,
        "Hull notes",
        folder="MuddSub / OBSOLETE / Alfie",
        modified="2021-09-04",
        chunks=["The latch on Alfie was a toggle."],
        drive_id="oldDoc000001",
    )
    import drive_index
    import drive_rank

    drive_index.demote_rules = lambda: drive_rank.load_rules()  # the real rules

    found = retrieval.retrieve(session, task, "latch")
    text = retrieval.user_content(found, "Which latch?")

    assert "[S1] Hull notes (doc)" in text
    assert "folder: MuddSub / OBSOLETE / Alfie" in text
    assert "modified: 2021-09-04" in text
    assert "possibly old:" in text
    assert "file_id (for read_file): oldDoc000001" in text
    assert "## Member's message\nWhich latch?" in text


def test_only_google_docs_can_be_read_live(session, task):
    add_file(session, "Latch notes", chunks=["latch"], drive_id="docId0000001")
    add_file(
        session, "Latch sheet", kind="sheet", chunks=["latch"], drive_id="sheetId00001"
    )

    found = retrieval.retrieve(session, task, "latch")

    assert found.readable_ids == {"docId0000001"}
    assert "sheetId00001" not in retrieval.user_content(found, "latch")


def test_nothing_matching_still_gives_an_honest_prompt(session, task):
    add_file(session, "Unrelated", chunks=["sponsors and banquets"])

    found = retrieval.retrieve(session, task, "zzzz qqqq")

    text = retrieval.user_content(found, "zzzz qqqq")
    assert found.covers_nothing or found.chunks == []
    assert "(nothing in the Drive index matched)" in text
    assert "(none on record for this subsystem)" in text


def test_earlier_sources_are_reloaded_and_carried_into_a_follow_up(session, task):
    row = add_file(
        session,
        "Process Router",
        chunks=["Step one: waterjet the blank.", "Step two: deburr."],
        drive_id="routerDoc0001",
    )
    chunk_id = session.exec(
        select(DriveChunk.id).where(DriveChunk.file_id == row.id)
    ).first()
    add_file(session, "Other", chunks=["something else entirely"])
    saved = [
        {
            "title": "Process Router",
            "link": row.web_link,
            "modified": "2026-10-05",
            "drive_id": "routerDoc0001",
            "chunk_ids": [chunk_id],
        }
    ]

    # The follow-up shares no words with the document.
    found = retrieval.retrieve(session, task, "where is it?", earlier_sources=saved)

    assert [h.name for h in found.earlier] == ["Process Router"]
    text = retrieval.user_content(found, "where is it?")
    assert "Sources cited earlier in this conversation" in text
    assert "waterjet the blank" in text
    assert row.web_link in found.known  # and it may be cited again


def test_a_saved_link_stays_citable_even_if_its_file_left_the_index(session, task):
    add_file(session, "Other", chunks=["other"])
    saved = [
        {
            "title": "Gone doc",
            "link": "https://docs.google.com/document/d/1GoneFromIndex00/edit",
            "drive_id": "goneFromIndex",
            "chunk_ids": [999],
        }
    ]

    found = retrieval.retrieve(session, task, "other", earlier_sources=saved)

    assert found.earlier == []
    assert saved[0]["link"] in found.known


def test_a_chunk_already_found_is_not_repeated_as_an_earlier_source(session, task):
    row = add_file(
        session, "Latch notes", chunks=["latch details"], drive_id="latchDoc0001"
    )
    first = retrieval.retrieve(session, task, "latch")
    saved = [retrieval.source_entry(first.chunks[0])]

    again = retrieval.retrieve(session, task, "latch", earlier_sources=saved)

    assert again.earlier == []
    assert row.web_link in again.known


# --- CAD names only when the question is about files ---


@pytest.mark.parametrize(
    "question",
    [
        "Where is the latest Crush enclosure assembly CAD?",
        "Which latch do we use on the lid?",
        "How do I start?",
        "Is there a newer version of the lid?",
    ],
)
def test_file_questions_get_cad_names(question):
    assert retrieval.wants_cad(question)


@pytest.mark.parametrize(
    "question",
    [
        "What clearance should the baseplate gasket have?",
        "Why did we go with a flat gasket instead of a grooved one?",
        "What lubricant should I use on the O-rings?",
    ],
)
def test_other_questions_do_not(question):
    assert not retrieval.wants_cad(question)


def test_a_short_follow_up_inherits_whether_the_chat_is_about_files():
    assert retrieval.wants_cad("and the second one?", "Where is the CAD file?")
    assert not retrieval.wants_cad("and the second one?", "What clearance?")


def test_cad_names_are_left_out_of_a_question_about_a_value(session, task):
    add_file(session, "Gasket notes", chunks=["gasket clearance is .030"])
    add_file(session, "Gasket_v3.SLDPRT", kind="cad", modified="2026-10-09")

    found = retrieval.retrieve(session, task, "What gasket clearance?")

    assert found.cad == []
    assert "Newest matching CAD files" not in retrieval.user_content(
        found, "What gasket clearance?"
    )


# --- each chunk is cut to the words around the match ---


def test_a_long_chunk_is_cut_to_about_150_words_around_the_match():
    body = " ".join(f"filler{i}" for i in range(600))
    body = body.replace("filler300", "clearance").replace("filler301", "gasket")

    shown = retrieval.excerpt(body, ["gasket", "clearance"])

    words = shown.split()
    assert 150 <= len(words) <= 152  # the 150 words plus two "…"
    assert "clearance" in shown and "gasket" in shown
    assert shown.startswith("…") and shown.endswith("…")
    assert "filler0 " not in shown


def test_the_excerpt_goes_where_the_matching_words_are_densest():
    body = " ".join(f"w{i}" for i in range(500))
    words = body.split()
    words[20] = "latch"  # one lone match early
    for i in (400, 405, 410, 415):
        words[i] = "latch"
    body = " ".join(words)

    assert "w416" in retrieval.excerpt(body, ["latch"])


def test_a_short_chunk_and_line_breaks_are_kept_whole():
    body = "Header: a\nSecond: b\n\nThird: c"

    assert retrieval.excerpt(body, ["a"]) == body


def test_a_chunk_without_a_matching_word_starts_at_its_top():
    body = " ".join(f"w{i}" for i in range(400))

    shown = retrieval.excerpt(body, ["zzz"])

    assert shown.startswith("w0 ") and shown.endswith("…")


# --- outlines of the top files ---


def put_chunks(session, file, chunks):
    from models import DriveChunk

    for position, (heading, text) in enumerate(chunks):
        session.add(
            DriveChunk(file_id=file.id, position=position, heading=heading, text=text)
        )
    session.commit()


def test_the_top_files_get_a_list_of_their_headings(session, task):
    file = add_file(session, "Fab Guidelines and Process Routers")
    put_chunks(
        session,
        file,
        [
            (
                "Tab 13",
                "Intro line about routers\nStep one\n\nFront Panel Process Router\nCut stock to size",
            ),
            (
                "Tab 12",
                "Cut metal to size, then mill\n\nRear Panel Process Router (in progress)\nTBD\n\nBaseplate Process Router\nDrill dowel holes",
            ),
        ],
    )

    found = retrieval.retrieve(session, task, "routers")
    text = retrieval.user_content(found, "routers")

    [(name, total, headings)] = found.outlines
    assert name == "Fab Guidelines and Process Routers" and total == 5
    assert headings == [
        "Tab 13",
        "Front Panel Process Router",
        "Tab 12",
        "Rear Panel Process Router (in progress)",
        "Baseplate Process Router",
    ]
    assert "## Outline of the top documents" in text
    assert "Rear Panel Process Router (in progress)" in text


def test_only_the_top_three_files_are_outlined_and_one_heading_is_no_outline(
    session, task
):
    for n in range(5):
        file = add_file(session, f"Router doc {n}", drive_id=f"router{n:07d}")
        put_chunks(session, file, [(f"A{n}", "router words"), (f"B{n}", "router more")])
    single = add_file(session, "Single section router", drive_id="single000001")
    put_chunks(session, single, [("Only", "router")])

    found = retrieval.retrieve(session, task, "router")

    assert len(found.outlines) <= retrieval.OUTLINE_FILES
    assert all(name != "Single section router" for name, _, _ in found.outlines)


def test_a_table_row_or_sentence_is_not_a_heading():
    for line in (
        "Process | Tool | Comments",
        "Clearance: .030; Material: Buna-N",
        "Cut metal to size and then face it with the flycutter at low depth of cut.",
        "",
    ):
        assert not retrieval.looks_like_heading(line)
    assert retrieval.looks_like_heading("Baseplate Process Router")


# --- sheet rows for a question about a value ---


def sheet_with_rows(session):
    file = add_file(session, "Seal Calculations", kind="sheet", drive_id="sealSheet001")
    put_chunks(
        session,
        file,
        [
            (
                "Gasket",
                (
                    "Part: baseplate gasket; Clearance: .030 in; Source: seal notes\n"
                    "Part: window gasket; Clearance: .050 in; Source: old sheet\n"
                    "Part: lid gasket; Material: Buna-N"
                ),
            )
        ],
    )
    return file


def test_a_question_about_a_value_adds_the_best_sheet_rows_with_their_tab(
    session, task
):
    sheet_with_rows(session)

    found = retrieval.retrieve(session, task, "What clearance should the gasket have?")
    text = retrieval.user_content(found, "What clearance should the gasket have?")

    assert 1 <= len(found.rows) <= retrieval.SHEET_ROWS
    assert all(hit.heading == "Gasket" for hit in found.rows)
    assert "## Matching sheet rows" in text or ".030 in" in text
    assert "Clearance: .030 in" in text


def test_a_row_the_excerpt_already_shows_is_not_repeated(session, task):
    sheet_with_rows(session)

    found = retrieval.retrieve(session, task, "What clearance should the gasket have?")
    text = retrieval.user_content(found, "What clearance should the gasket have?")

    assert text.count("Part: baseplate gasket; Clearance: .030 in") == 1


def test_no_quantity_word_means_no_row_search(session, task):
    sheet_with_rows(session)

    found = retrieval.retrieve(session, task, "Which gasket material do we use?")

    assert found.rows == []


@pytest.mark.parametrize(
    "question",
    [
        "What clearance should the gasket have?",
        "How thick is the window stock?",
        "What diameter is the dowel?",
        "What size screw holds the lid?",
        "What torque for the thruster bolts?",
        "How tall is the enclosure?",
    ],
)
def test_quantity_words_in_their_common_forms(question):
    assert retrieval.asks_for_a_quantity(question)
    assert not retrieval.asks_for_a_quantity("Why did we pick a flat gasket?")


def test_a_row_from_a_tab_merged_into_a_chunk_gets_its_own_tab_name(session, task):
    file = add_file(session, "Window specs", kind="sheet", drive_id="winSheet0001")
    put_chunks(
        session,
        file,
        [
            (
                "Overview",
                "Part: lid; Note: see tabs\n\nWindow\nStock: acrylic; Thickness: .226 in",
            )
        ],
    )

    found = retrieval.retrieve(session, task, "How thick is the window stock?")

    assert [(h.heading, "Thickness: .226 in" in h.text) for h in found.rows] == [
        ("Window", True)
    ]


# --- a vague question searches the task's own words like its own ---


@pytest.mark.parametrize(
    "question",
    ["How do I start?", "Where do I begin?", "Can you explain in more detail?"],
)
def test_a_question_that_says_nothing_about_what_uses_the_title_as_the_query(
    task, question
):
    query, boost = retrieval.search_terms(task, question)

    assert "Finish enclosure wall panels" in query and "Enclosure" in query
    assert boost == ""


@pytest.mark.parametrize(
    "question",
    ["Which latch?", "What clearance should the gasket have?"],
)
def test_a_question_about_something_keeps_the_title_as_a_boost(task, question):
    query, boost = retrieval.search_terms(task, question)

    assert query == question and "Finish enclosure wall panels" in boost


def test_how_do_i_start_finds_the_tasks_cad_file_by_its_title(session):
    electronics = Task(
        code="E01",
        title="Revise electronics enclosure subassembly",
        subsystem="Electronics Mounts",
        task_type="Design",
        stage="requirements",
    )
    add_file(session, "Notes about sponsors", chunks=["start of season banquet"])
    add_file(
        session,
        "MdSb26_ElectronicsEnclosure_SubAssem.SLDASM",
        kind="cad",
        modified="2026-10-09",
    )

    found = retrieval.retrieve(session, electronics, "How do I start?")

    assert "MdSb26_ElectronicsEnclosure_SubAssem.SLDASM" in [h.name for h in found.cad]


# --- a window for the far end of a chunk too ---


def test_a_second_window_catches_a_matching_word_far_from_the_first():
    words = [f"w{i}" for i in range(500)]
    for i in range(40, 50):
        words[i] = "spring"  # many matches of one word near the start
    words[400] = "compressor"  # the one place the reason is written
    body = " ".join(words)

    shown = retrieval.excerpt(body, ["spring", "compressed"])

    assert "compressor" in shown and "spring" in shown
    assert 150 <= len(shown.split()) <= 160  # still about 150 words in all
    assert shown.count("…") >= 2


def test_one_region_of_matches_gets_the_whole_budget():
    words = [f"w{i}" for i in range(500)]
    for i in range(300, 306):
        words[i] = "latch"

    shown = retrieval.excerpt(" ".join(words), ["latch"])

    assert shown.count("latch") == 6 and len(shown.split()) <= 152
    assert "\n… \n" not in shown  # one window, not two
