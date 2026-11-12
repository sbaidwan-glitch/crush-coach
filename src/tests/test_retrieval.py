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


def test_the_query_has_the_question_the_task_and_the_subsystem(task):
    query = retrieval.search_query(task, "Which latch?")

    assert query == "Which latch? Finish enclosure wall panels Enclosure"


def test_a_short_follow_up_is_searched_with_the_last_question(task):
    short = retrieval.search_query(task, "and the second one?", "Which latch?")
    long = retrieval.search_query(
        task,
        "Please tell me about the process router for the side panel",
        "Which latch?",
    )

    assert "Which latch?" in short
    assert "Which latch?" not in long


def test_chunks_are_ranked_two_per_file_and_capped_at_twelve(session, task):
    add_file(session, "Latch notes", chunks=[f"latch detail {i}" for i in range(5)])
    for i in range(14):
        add_file(
            session,
            f"Doc {i}",
            chunks=[f"latch mention number {i}"],
            drive_id=f"doc{i:010d}",
        )

    found = retrieval.retrieve(session, task, "latch")

    assert len(found.chunks) == retrieval.CHUNK_LIMIT == 12
    per_file = {}
    for hit in found.chunks:
        per_file[hit.file_id] = per_file.get(hit.file_id, 0) + 1
    assert max(per_file.values()) == retrieval.MAX_PER_FILE == 2


def test_cad_files_are_names_only_and_newest_first(session, task):
    add_file(session, "Latch notes", chunks=["latch"])
    add_file(session, "Enclosure_v1.SLDPRT", kind="cad", modified="2026-01-01")
    add_file(session, "Enclosure_v3.SLDPRT", kind="cad", modified="2026-10-09")
    add_file(session, "Enclosure_v2.SLDPRT", kind="cad", modified="2026-05-01")

    found = retrieval.retrieve(session, task, "enclosure")

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
    assert found.known[f"/decisions/{mine.id}"]["title"].startswith("Decision D")
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
