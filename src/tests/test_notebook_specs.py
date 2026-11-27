"""Suggest specs: one button, one model run over the recommendation machinery, specs that all
carry a source, the member ticking which to keep, and the counts that measure it. Made-up files;
the model is a script."""

import json

import fake_openai as fake
import pytest
from fake_google import http_error
from index_helpers import add_file
from notebook_helpers import HTMX, LEAD, MEMBER, add_task, stored, use_model
from sqlmodel import select

import llm
import notebook
from models import DriveWrite, LlmCall, NoteSpec, WorkNote

SEAL = "https://docs.google.com/document/d/1SealCalcSealCalcSeal/edit"
HANDBOOK = "https://drive.google.com/file/d/1HandbookHandbookHandbook/view"
DID = "Machined the baseplate gasket groove with a 6 mm cross-section o-ring in mind."
WHY = "The seal calculations say the groove must match the squeeze."


@pytest.fixture
def index(session):
    add_file(
        session,
        "Seal Calculations",
        folder="MuddSub / 2025-2026 / Mechanical",
        modified="2026-10-05",
        chunks=["Baseplate gasket groove depth is 4.8 mm for a 6 mm cross-section o-ring (static face seal)."],
        drive_id="1SealCalcSealCalcSeal",
        link=SEAL,
    )
    add_file(
        session,
        "Parker O-Ring Handbook.pdf",
        kind="pdf",
        folder="MuddSub / Library / Textbooks",
        chunks=["Static face seal squeeze should be 15 to 30 percent of the cross section."],
        drive_id="handbook0001",
        link=HANDBOOK,
    )


def spec(text, link=SEAL, unit="mm", title="whatever the model says"):
    return {"text": text, "unit": unit, "source_title": title, "source_link": link}


def reply(*specs):
    return fake.reply(json.dumps({"specs": list(specs)}), input_tokens=3000, output_tokens=200)


def ask_for_specs(client, **extra):
    data = {"task": "M06", "did": DID, "why": WHY, **extra}
    return client.post("/notebook/entry/specs", data=data, headers=HTMX)


def specs_of(session):
    session.expire_all()
    return list(session.exec(select(NoteSpec).order_by(NoteSpec.position)).all())


def test_the_button_makes_one_run_with_the_reference_tools_and_shows_each_spec_with_unit_and_source(
    session, as_user, index, monkeypatch
):
    add_task(session)
    model = use_model(monkeypatch, reply(spec("Groove depth, 6 mm o-ring", unit="mm"), spec("Squeeze", HANDBOOK, "%")))

    response = ask_for_specs(as_user(MEMBER))

    assert response.status_code == 200 and len(model.requests) == 1
    request = model.requests[0]
    assert {t["function"]["name"] for t in request["tools"]} == {"search_references", "calculate"}
    prompt = request["messages"][-1]["content"]
    assert "Baseplate gasket groove depth is 4.8 mm" in prompt  # the same retrieval the coach uses
    assert "Suggest specs mode" in request["messages"][0]["content"]
    assert DID in prompt and WHY in prompt
    html = response.text
    assert "2 specs suggested. Tick the ones to keep." in html
    assert 'value="Groove depth, 6 mm o-ring"' in html and "Unit: mm" in html
    assert f'href="{SEAL}"' in html and ">Seal Calculations</a>" in html  # the title comes from the source
    assert f'href="{HANDBOOK}"' in html and "Parker O-Ring Handbook.pdf" in html
    assert 'type="checkbox"' in html and " checked" not in html.split("Suggested specs")[1].split("</fieldset>")[0]


def test_every_spec_is_saved_unticked_with_the_models_own_words_for_later(session, as_user, index, monkeypatch):
    add_task(session)
    use_model(monkeypatch, reply(spec("Groove depth, 6 mm o-ring"), spec("Squeeze 15 to 30 percent", HANDBOOK, "%")))

    ask_for_specs(as_user(MEMBER))

    note = stored(session, WorkNote)[0]
    assert note.state == "draft" and note.did == DID  # the entry was saved so the specs have a home
    saved = specs_of(session)
    assert [(s.text, s.original_text, s.unit, s.accepted, s.edited) for s in saved] == [
        ("Groove depth, 6 mm o-ring", "Groove depth, 6 mm o-ring", "mm", False, False),
        ("Squeeze 15 to 30 percent", "Squeeze 15 to 30 percent", "%", False, False),
    ]
    assert [(s.source_title, s.source_link) for s in saved] == [
        ("Seal Calculations", SEAL), ("Parker O-Ring Handbook.pdf", HANDBOOK),
    ]
    assert all(s.note_id == note.id for s in saved)


def test_a_suggestion_with_no_source_is_dropped_and_the_page_says_how_many(session, as_user, index, monkeypatch):
    add_task(session)
    invented = "https://docs.google.com/document/d/1MadeUpMadeUpMadeUp/edit"
    use_model(
        monkeypatch,
        reply(
            spec("Groove depth, 6 mm o-ring"),
            {"text": "Torque 4 N·m", "unit": "N·m", "source_title": "", "source_link": ""},
            spec("Invented value", invented),
            {"text": "No link key at all", "unit": "mm"},
            "not even an object",
        ),
    )

    response = ask_for_specs(as_user(MEMBER))

    assert [s.text for s in specs_of(session)] == ["Groove depth, 6 mm o-ring"]
    assert "4 suggestions dropped: no source to open." in response.text
    assert "Torque 4" not in response.text and "Invented value" not in response.text
    assert stored(session, WorkNote)[0].specs_dropped == 4


def test_one_dropped_suggestion_is_singular(session, as_user, index, monkeypatch):
    add_task(session)
    use_model(monkeypatch, reply(spec("Fine"), spec("Not fine", "")))

    assert "1 suggestion dropped: no source to open." in ask_for_specs(as_user(MEMBER)).text


def test_when_nothing_has_a_source_it_says_so_and_still_counts_what_was_dropped(session, as_user, index, monkeypatch):
    add_task(session)
    use_model(monkeypatch, reply(spec("Guess", ""), spec("Another guess", "https://example.com/x")))

    response = ask_for_specs(as_user(MEMBER))

    assert "No suggestion had a source, so there is nothing to add." in response.text
    assert "2 suggestions dropped: no source to open." in response.text
    assert specs_of(session) == []


def test_a_link_to_the_same_drive_file_in_another_shape_is_a_source(session, as_user, index, monkeypatch):
    add_task(session)
    other_shape = "https://drive.google.com/file/d/1SealCalcSealCalcSeal/view"
    use_model(monkeypatch, reply(spec("Groove depth", other_shape)))

    ask_for_specs(as_user(MEMBER))

    (saved,) = specs_of(session)
    assert saved.source_link == SEAL and saved.source_title == "Seal Calculations"


def test_the_run_is_logged_with_its_cost_and_purpose(session, as_user, index, monkeypatch):
    task = add_task(session)
    use_model(monkeypatch, reply(spec("Groove depth")))

    ask_for_specs(as_user(MEMBER))

    (call,) = stored(session, LlmCall)
    assert (call.purpose, call.status, call.member, call.task_id) == ("specs", "ok", "sam@example.edu", task.id)
    assert (call.input_tokens, call.output_tokens) == (3000, 200) and call.cost == pytest.approx(0.0012)


def test_the_run_may_use_the_calculator_and_every_call_is_logged(session, as_user, index, monkeypatch):
    add_task(session)
    model = use_model(
        monkeypatch,
        fake.tool_request(("c1", "calculate", '{"expression": "6 * 0.8", "what": "groove depth"}')),
        reply(spec("Groove depth 4.8", unit="mm")),
    )

    response = ask_for_specs(as_user(MEMBER))

    assert len(model.requests) == 2 and "4.8" in response.text
    assert [c.purpose for c in stored(session, LlmCall)] == ["specs", "specs"]
    tool_answer = next(m for m in model.requests[1]["messages"] if m["role"] == "tool")
    assert "4.8" in tool_answer["content"]


def test_a_failed_run_says_why_keeps_the_draft_and_logs_the_attempt(session, as_user, index, monkeypatch):
    add_task(session)
    use_model(monkeypatch, fake.timed_out())

    response = ask_for_specs(as_user(MEMBER))

    assert response.status_code == 502 and "Took too long" in response.text
    assert stored(session, WorkNote)[0].did == DID and specs_of(session) == []
    (call,) = stored(session, LlmCall)
    assert call.status == "timeout" and call.purpose == "specs"
    assert DID in response.text  # what they typed is still in the box


def test_a_reply_that_is_not_a_list_is_an_error_not_a_crash(session, as_user, index, monkeypatch):
    add_task(session)
    use_model(monkeypatch, fake.reply("Sure! Here are some specs: depth 5."))

    response = ask_for_specs(as_user(MEMBER))

    assert response.status_code == 502 and "list specs this time" in response.text


def test_a_model_error_does_not_leak_anything(session, as_user, index, monkeypatch):
    add_task(session)
    use_model(monkeypatch, fake.server_error())

    assert "sk-secret-123" not in ask_for_specs(as_user(MEMBER)).text


def test_without_an_index_it_says_to_ask_a_lead_and_calls_no_model(session, as_user, monkeypatch):
    add_task(session)
    model = use_model(monkeypatch)

    response = ask_for_specs(as_user(MEMBER))

    assert "Drive index is empty" in response.text and model.requests == []


def test_nothing_written_means_no_model_call(session, as_user, index, monkeypatch):
    add_task(session)
    model = use_model(monkeypatch)

    response = ask_for_specs(as_user(MEMBER), did="  ", why="")

    assert response.status_code == 400 and model.requests == []


def test_a_run_counts_once_whatever_it_looked_up_and_the_limit_stops_the_next(
    session, as_user, index, monkeypatch
):
    add_task(session)
    monkeypatch.setenv("SPECS_DAILY_LIMIT", "1")
    model = use_model(
        monkeypatch,
        fake.tool_request(("c1", "calculate", '{"expression": "1+1"}')),
        reply(spec("Groove depth")),
    )
    client = as_user(MEMBER)

    first = ask_for_specs(client)
    blocked = ask_for_specs(client)

    assert first.status_code == 200 and len(model.requests) == 2  # one run, two calls
    assert blocked.status_code == 400 and "used today's 1 spec suggestions" in blocked.text.replace("&#39;", "'")
    assert len(model.requests) == 2
    assert notebook.specs_today(session, "sam@example.edu") == 1


def test_asking_again_replaces_the_list(session, as_user, index, monkeypatch):
    add_task(session)
    use_model(monkeypatch, reply(spec("First list")), reply(spec("Second list")))
    client = as_user(MEMBER)
    ask_for_specs(client)
    entry = stored(session, WorkNote)[0].id

    ask_for_specs(client, entry=str(entry))

    assert [s.text for s in specs_of(session)] == ["Second list"]


# --- ticking, editing, publishing, counting -------------------------------------------------------------


def run_and_choose(session, client, monkeypatch, keep, edits=None, three=True):
    use_model(
        monkeypatch,
        reply(spec("Groove depth 4.8"), spec("Squeeze 15 to 30 percent", HANDBOOK, "%"), spec("Gasket width 8", SEAL, "mm")),
    )
    ask_for_specs(client)
    saved = specs_of(session)
    data = {"task": "M06", "did": DID, "why": WHY, "entry": str(saved[0].note_id), "keep": [str(saved[i].id) for i in keep]}
    for index, text in (edits or {}).items():
        data[f"spec_text_{saved[index].id}"] = text
    return saved, data


def test_only_ticked_specs_are_kept_and_an_edit_is_recorded(session, as_user, index, doc, monkeypatch):
    add_task(session)
    client = as_user(MEMBER)
    _, data = run_and_choose(session, client, monkeypatch, keep=[0, 1], edits={1: "Squeeze 20 percent (team choice)"})

    response = client.post("/notebook/entry/publish", data=data, headers=HTMX)

    assert "Published. Written to the Enclosure tab" in response.text
    after = specs_of(session)
    assert [(s.accepted, s.edited) for s in after] == [(True, False), (True, True), (False, False)]
    assert after[1].text == "Squeeze 20 percent (team choice)" and after[1].original_text == "Squeeze 15 to 30 percent"
    lines = doc.lines("Enclosure")
    assert lines[-3].startswith("Sam Example · M06:")
    assert lines[-2] == "Spec: Groove depth 4.8 mm. Source: Seal Calculations"
    assert lines[-1] == "Spec: Squeeze 20 percent (team choice) %. Source: Parker O-Ring Handbook.pdf"
    assert "Gasket width" not in doc.tab("Enclosure").text()  # not ticked
    assert doc.links("Enclosure") == {SEAL: "Seal Calculations", HANDBOOK: "Parker O-Ring Handbook.pdf"}


def test_saving_a_draft_keeps_the_ticks_and_edits_for_later(session, as_user, index, monkeypatch):
    add_task(session)
    client = as_user(MEMBER)
    _, data = run_and_choose(session, client, monkeypatch, keep=[2], edits={2: "Gasket width 9"})

    response = client.post("/notebook/entry/save", data=data, headers=HTMX)

    assert "Draft saved." in response.text
    assert [(s.accepted, s.edited) for s in specs_of(session)] == [(False, False), (False, False), (True, True)]
    assert 'value="Gasket width 9"' in response.text and response.text.count(" checked") >= 1


def test_the_counts_for_the_write_up_split_accepted_edited_and_rejected(session, as_user, index, doc, monkeypatch):
    add_task(session)
    client = as_user(MEMBER)
    _, data = run_and_choose(session, client, monkeypatch, keep=[0, 1], edits={1: "Squeeze 20 percent"})
    # A suggestion that had no source is counted too, but never offered.
    use_model(monkeypatch, reply(spec("Fine"), spec("No source", "")))
    ask_for_specs(client, task="M06")  # a second draft; unpublished, so it does not count yet
    client.post("/notebook/entry/publish", data=data, headers=HTMX)

    assert notebook.spec_counts(session) == {
        "entries": 1, "suggested": 3, "accepted": 1, "edited": 1, "rejected": 1, "dropped": 0,
    }


def test_the_lead_sees_the_counts_on_admin_and_members_cannot(session, as_user, index, doc, monkeypatch):
    add_task(session)
    client = as_user(MEMBER)
    _, data = run_and_choose(session, client, monkeypatch, keep=[0])
    client.post("/notebook/entry/publish", data=data, headers=HTMX)

    page = as_user(LEAD).get("/admin").text

    assert "Suggested specs" in page and "<small>Accepted</small><b>1</b>" in page
    assert "<small>Rejected</small><b>2</b>" in page and "3 suggested with a source across 1 published entry" in page
    assert as_user(MEMBER).get("/admin").status_code == 403


def test_a_spec_a_member_never_ticked_is_not_in_the_record_or_the_doc(session, as_user, index, doc, monkeypatch):
    add_task(session)
    client = as_user(MEMBER)
    _, data = run_and_choose(session, client, monkeypatch, keep=[])

    client.post("/notebook/entry/publish", data=data, headers=HTMX)

    record = as_user(LEAD).get("/record?notes=1").text
    assert "Groove depth 4.8" not in record and "Spec:" not in doc.tab("Enclosure").text()
    assert [w.kind for w in stored(session, DriveWrite)] == ["note"]


def test_the_record_shows_accepted_specs_with_their_source(session, as_user, index, doc, monkeypatch):
    add_task(session)
    client = as_user(MEMBER)
    _, data = run_and_choose(session, client, monkeypatch, keep=[0])
    client.post("/notebook/entry/publish", data=data, headers=HTMX)

    record = as_user(LEAD).get("/record?notes=1").text

    assert "Spec: Groove depth 4.8 mm. Source:" in record and f'href="{SEAL}"' in record


def test_the_prompt_file_asks_for_sources_and_json():
    prompt = llm.specs_prompt()
    assert "needs a source" in prompt and '"specs"' in prompt and "source_link" in prompt


def test_publishing_and_saving_never_call_the_model(session, as_user, index, doc, monkeypatch):
    add_task(session)
    model = use_model(monkeypatch)
    client = as_user(MEMBER)

    client.post("/notebook/entry/save", data={"task": "M06", "did": DID}, headers=HTMX)
    client.post("/notebook/entry/publish", data={"task": "M06", "did": DID}, headers=HTMX)

    assert model.requests == []


def test_http_errors_on_the_docs_side_are_unrelated_to_specs(session, as_user, index, doc, monkeypatch):
    add_task(session)
    doc.fail_get = http_error(503, "x")
    client = as_user(MEMBER)
    _, data = run_and_choose(session, client, monkeypatch, keep=[0])

    response = client.post("/notebook/entry/publish", data=data, headers=HTMX)

    assert "Failed to publish" in response.text
    assert [s.accepted for s in specs_of(session)] == [True, False, False]  # the choice is kept
