"""The model module, with a scripted fake client and a fake Drive: no network."""

import json

import fake_openai as fake
import pytest

import drive
import llm

DOC_LINK = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"
OLD_LINK = "https://docs.google.com/document/d/9ZyXwVuTsRqPoNmL/edit"
TASK = {
    "Code": "M04",
    "Title": "Finish enclosure wall panels",
    "Subsystem": "Enclosure",
}


def search_result(
    link=DOC_LINK,
    title="Latch design notes",
    modified="2026-09-30T12:00:00Z",
    folder="Crush / Enclosure",
):
    location = {
        "link": link,
        "mime_type": "application/vnd.google-apps.document",
        "modified_time": modified,
        "folder_path": folder,
    }
    return {
        "title": title,
        "link": link,
        "mime_type": location["mime_type"],
        "modified_time": modified,
        "folder_path": folder,
        "locations": [location],
    }


@pytest.fixture
def drive_calls(monkeypatch):
    calls = {"search": [], "read": []}

    def fake_search(query, limit=10):
        calls["search"].append(query)
        return [search_result()]

    def fake_read(file_id):
        calls["read"].append(file_id)
        return "Use the two-point latch. Source of truth for Crush."

    monkeypatch.setattr(drive, "search", fake_search)
    monkeypatch.setattr(drive, "read_text", fake_read)
    return calls


def use_client(monkeypatch, script):
    client = fake.FakeClient(script)
    monkeypatch.setattr(llm, "get_client", lambda: client)
    return client


def search_call(call_id="c1", query="latch"):
    return (call_id, "drive_search", json.dumps({"query": query}))


def read_call(call_id="c2", link=DOC_LINK):
    return (call_id, "drive_read_text", json.dumps({"link": link}))


def test_tool_call_flow_searches_reads_and_answers(monkeypatch, drive_calls):
    client = use_client(
        monkeypatch,
        [
            fake.tool_request(search_call()),
            fake.tool_request(read_call()),
            fake.reply(f"What do you need from the latch? See {DOC_LINK} (2026)."),
        ],
    )
    records = []

    answer = llm.run_coach(TASK, [], "How do I do the latch?", record=records.append)

    assert drive_calls == {"search": ["latch"], "read": ["1AbCdEfGhIjKlMnOp"]}
    assert answer.tool_calls == 2
    assert answer.text.startswith("What do you need")
    assert answer.sources == [
        {"title": "Latch design notes", "link": DOC_LINK, "modified": "2026-09-30"}
    ]
    # Three model calls, each logged with tokens and the proxy-reported cost.
    assert [r.status for r in records] == ["ok", "ok", "ok"]
    assert records[0].input_tokens == 80 and records[0].output_tokens == 15
    assert records[2].cost == pytest.approx(0.0012)
    assert all(r.model == "claude-haiku-4-5" for r in records)
    # Tool results were sent back to the model, tied to the right call ids.
    second = client.requests[1]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "drive_search"
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "c1"
    assert "Latch design notes" in second[-1]["content"]
    assert client.requests[0]["tools"][0]["function"]["name"] == "drive_search"
    assert "tool_choice" not in client.requests[0]


def test_system_prompt_and_task_are_sent(monkeypatch, drive_calls):
    client = use_client(monkeypatch, [fake.reply("Hi")])

    llm.run_coach(TASK, [{"role": "user", "content": "earlier"}], "now")

    system = client.requests[0]["messages"][0]
    assert system["role"] == "system"
    assert "never decide" in system["content"]
    assert "M04" in system["content"]
    roles = [m["role"] for m in client.requests[0]["messages"]]
    assert roles == ["system", "user", "user"]


def test_at_most_four_tool_calls_per_answer(monkeypatch, drive_calls):
    client = use_client(
        monkeypatch,
        [
            fake.tool_request(search_call("a"), search_call("b")),
            fake.tool_request(search_call("c"), search_call("d"), search_call("e")),
            fake.reply("Here is what I found."),
        ],
    )

    answer = llm.run_coach(TASK, [], "latch?")

    assert len(drive_calls["search"]) == 4  # the fifth request was refused
    assert answer.tool_calls == 4
    tool_results = [m for m in client.requests[2]["messages"] if m["role"] == "tool"]
    assert len(tool_results) == 5
    assert "Tool limit reached" in tool_results[-1]["content"]
    # Once the limit is hit the model must answer: more tools are forbidden.
    assert client.requests[2]["tool_choice"] == "none"
    assert "tool_choice" not in client.requests[1]


def test_model_that_ignores_tool_choice_gets_a_fallback_answer(
    monkeypatch, drive_calls
):
    use_client(
        monkeypatch,
        [
            fake.tool_request(*[search_call(f"c{i}") for i in range(4)]),
            fake.tool_request(search_call("again")),
        ],
    )

    answer = llm.run_coach(TASK, [], "latch?")

    assert len(drive_calls["search"]) == 4
    assert "couldn't put an answer together" in answer.text


@pytest.mark.parametrize(
    ("failure", "kind", "message"),
    [
        (fake.budget_exceeded, "budget_exceeded", "AI budget is used up"),
        (fake.rate_limited, "rate_limited", "rate limit"),
        (fake.timed_out, "timeout", "took too long"),
        (fake.unreachable, "timeout", "couldn't be reached"),
        (fake.key_rejected, "error", "key was rejected"),
        (fake.server_error, "error", "failed to answer"),
    ],
)
def test_model_failures_become_plain_logged_errors(
    monkeypatch, caplog, failure, kind, message
):
    use_client(monkeypatch, [failure()])
    records = []

    with pytest.raises(llm.LLMError) as excinfo:
        llm.run_coach(TASK, [], "latch?", record=records.append)

    assert excinfo.value.kind == kind
    assert message in excinfo.value.message
    assert "sk-secret-123" not in excinfo.value.message
    assert [r.status for r in records] == [kind]  # failed calls are logged too
    assert "sk-secret-123" not in caplog.text
    assert "sk-test-not-a-real-key" not in caplog.text


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GLASSROOM_API_KEY")
    records = []

    with pytest.raises(llm.LLMError) as excinfo:
        llm.run_coach(TASK, [], "latch?", record=records.append)

    assert "isn't set up" in excinfo.value.message
    assert records[0].status == "error"


def test_client_uses_glassroom_endpoint_and_model_from_env(monkeypatch):
    client = llm.get_client()
    assert str(client.base_url) == "https://litellm.glassroomai.com/v1/"

    assert llm.model_name() == "claude-haiku-4-5"
    monkeypatch.setenv("COACH_MODEL", "some-other-model")
    assert llm.model_name() == "some-other-model"


def test_drive_failure_is_reported_and_the_chat_still_answers(monkeypatch):
    def broken_search(query, limit=10):
        raise drive.DriveError("Google Drive search failed. Check network access.")

    monkeypatch.setattr(drive, "search", broken_search)
    client = use_client(
        monkeypatch,
        [fake.tool_request(search_call()), fake.reply("Drive could not be reached.")],
    )

    answer = llm.run_coach(TASK, [], "latch?")

    assert answer.drive_failed is True
    assert answer.text == "Drive could not be reached."
    tool_result = client.requests[1]["messages"][-1]["content"]
    assert "Google Drive failed" in tool_result


def test_unexpected_drive_exception_is_contained(monkeypatch):
    def explode(query, limit=10):
        raise RuntimeError("token=sk-secret-123")

    monkeypatch.setattr(drive, "search", explode)
    client = use_client(
        monkeypatch, [fake.tool_request(search_call()), fake.reply("No Drive today.")]
    )

    answer = llm.run_coach(TASK, [], "latch?")

    assert answer.drive_failed is True
    assert "sk-secret-123" not in client.requests[1]["messages"][-1]["content"]


def test_search_results_flag_old_and_alfie_sources(monkeypatch):
    results = [
        search_result(
            OLD_LINK, "Old latch", "2022-03-01T00:00:00Z", "Alfie / Enclosure"
        ),
        search_result(DOC_LINK, "New latch", "2026-10-01T00:00:00Z", "Crush"),
    ]
    monkeypatch.setattr(drive, "search", lambda query, limit=10: results)
    client = use_client(
        monkeypatch, [fake.tool_request(search_call()), fake.reply("ok")]
    )

    llm.run_coach(TASK, [], "latch?")

    payload = json.loads(client.requests[1]["messages"][-1]["content"])
    old, new = payload["results"]
    assert old["possibly_old"] is True and old["folder_path"] == "Alfie / Enclosure"
    assert new["possibly_old"] is False
    assert old["modified"] == "2022-03-01"


def test_nothing_found_is_passed_on_honestly(monkeypatch):
    monkeypatch.setattr(drive, "search", lambda query, limit=10: [])
    client = use_client(
        monkeypatch,
        [fake.tool_request(search_call()), fake.reply("I found nothing on that.")],
    )

    answer = llm.run_coach(TASK, [], "latch?")

    assert "No matching documents" in client.requests[1]["messages"][-1]["content"]
    assert answer.sources == []


def test_long_documents_are_truncated(monkeypatch):
    monkeypatch.setattr(drive, "read_text", lambda file_id: "x" * 50_000)
    client = use_client(monkeypatch, [fake.tool_request(read_call()), fake.reply("ok")])

    llm.run_coach(TASK, [], "latch?")

    payload = json.loads(client.requests[1]["messages"][-1]["content"])
    assert len(payload["document_text_untrusted"]) == llm.MAX_DOC_CHARS
    assert payload["truncated"] is True


def test_bad_tool_arguments_do_not_crash(monkeypatch, drive_calls):
    client = use_client(
        monkeypatch,
        [
            fake.tool_request(
                ("c1", "drive_search", "{not json"), ("c2", "nope", "{}")
            ),
            fake.reply("ok"),
        ],
    )

    answer = llm.run_coach(TASK, [], "latch?")

    assert answer.text == "ok"
    assert drive_calls["search"] == []
    results = [
        m["content"] for m in client.requests[1]["messages"] if m["role"] == "tool"
    ]
    assert "not valid JSON" in results[0]
    assert "Unknown tool" in results[1]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (DOC_LINK, "1AbCdEfGhIjKlMnOp"),
        ("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp", "1AbCdEfGhIjKlMnOp"),
        ("1AbCdEfGhIjKlMnOp", "1AbCdEfGhIjKlMnOp"),
        ("not a link", None),
        ("", None),
    ],
)
def test_drive_file_id(value, expected):
    assert llm.drive_file_id(value) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Run it at 1200 rpm.",
        "Use a #4-40 tap.",
        "Hold ±0.005 on the bore.",
        "Drill 3 mm.",
    ],
)
def test_machining_numbers_without_a_source_are_flagged(text):
    assert llm.has_unsourced_numbers(text, []) is True
    assert llm.has_unsourced_numbers(text, [{"link": DOC_LINK}]) is False


def test_plain_advice_is_not_flagged():
    assert llm.has_unsourced_numbers("Which stage are you in?", []) is False


# --- find_decisions: confirmed decisions come before Drive ---

DECISION = {
    "id": 12,
    "link": "/decisions/12",
    "topic": "Latch style",
    "decision": "Use the two-point cam latch.",
    "confirmed": "2026-10-20",
    "sources": [
        {"title": "Latch trade study", "link": OLD_LINK, "folder": "Crush", "year": 2026}
    ],
}


def decisions_call(call_id="d1", query="latch"):
    return (call_id, "find_decisions", json.dumps({"query": query}))


def tool_names(request):
    return [t["function"]["name"] for t in request["tools"]]


def test_find_decisions_is_offered_only_when_the_caller_can_look_them_up(
    monkeypatch, drive_calls
):
    client = use_client(monkeypatch, [fake.reply("a"), fake.reply("b")])

    llm.run_coach(TASK, [], "latch?")
    llm.run_coach(TASK, [], "latch?", find_decisions=lambda q, s: [])

    assert tool_names(client.requests[0]) == ["drive_search", "drive_read_text"]
    assert tool_names(client.requests[1]) == [
        "drive_search",
        "drive_read_text",
        "find_decisions",
    ]


def test_drive_is_refused_until_decisions_were_checked(monkeypatch, drive_calls):
    lookups = []
    client = use_client(
        monkeypatch,
        [
            fake.tool_request(search_call("early")),
            fake.tool_request(decisions_call()),
            fake.tool_request(search_call("late")),
            fake.reply("Here is what I found."),
        ],
    )

    answer = llm.run_coach(
        TASK,
        [],
        "latch?",
        find_decisions=lambda q, s: lookups.append((q, s)) or [DECISION],
    )

    assert lookups == [("latch", "")]
    assert drive_calls["search"] == ["latch"]  # only the call after the check ran
    refused = client.requests[1]["messages"][-1]
    assert "Call find_decisions first" in refused["content"]
    assert "Latch design notes" not in refused["content"]
    assert answer.tool_calls == 3  # the refusal counts, so it can't loop forever


def test_the_decision_check_does_not_use_up_a_drive_call(monkeypatch, drive_calls):
    use_client(
        monkeypatch,
        [
            fake.tool_request(decisions_call(), search_call("a"), search_call("b")),
            fake.tool_request(search_call("c"), search_call("d"), search_call("e")),
            fake.reply("Done."),
        ],
    )

    answer = llm.run_coach(TASK, [], "latch?", find_decisions=lambda q, s: [])

    assert len(drive_calls["search"]) == 4  # still four Drive calls, plus the check
    assert answer.tool_calls == 5


def test_decisions_reach_the_model_and_conflicts_cite_both_links(
    monkeypatch, drive_calls
):
    client = use_client(
        monkeypatch,
        [
            fake.tool_request(decisions_call()),
            fake.tool_request(search_call()),
            fake.reply(
                "Decision /decisions/12 says two-point cam latch, but the Drive "
                f"notes {DOC_LINK} (2026) say toggle. The notes are possibly stale."
            ),
        ],
    )

    answer = llm.run_coach(TASK, [], "latch?", find_decisions=lambda q, s: [DECISION])

    result = json.loads(client.requests[1]["messages"][-1]["content"])
    assert result["confirmed_decisions"][0]["decision"] == "Use the two-point cam latch."
    assert result["confirmed_decisions"][0]["link"] == "/decisions/12"
    links = {s["link"] for s in answer.sources}
    assert links == {"/decisions/12", DOC_LINK}
    titles = {s["title"] for s in answer.sources}
    assert "Decision D12: Latch style" in titles


def test_a_decision_link_is_not_mistaken_for_a_longer_one(monkeypatch, drive_calls):
    use_client(
        monkeypatch,
        [fake.tool_request(decisions_call()), fake.reply("See /decisions/123 for it.")],
    )

    answer = llm.run_coach(TASK, [], "latch?", find_decisions=lambda q, s: [DECISION])

    assert answer.sources == []  # D12 was not cited; /decisions/123 is a different page


def test_a_failed_decision_lookup_is_reported_and_drive_still_works(
    monkeypatch, drive_calls, caplog
):
    def broken(query, subsystem):
        raise RuntimeError("postgresql://user:secret@host/db is down")

    client = use_client(
        monkeypatch,
        [
            fake.tool_request(decisions_call()),
            fake.tool_request(search_call()),
            fake.reply("I couldn't check the decisions."),
        ],
    )

    answer = llm.run_coach(TASK, [], "latch?", find_decisions=broken)

    assert answer.decisions_failed is True
    result = client.requests[1]["messages"][-1]["content"]
    assert "could not be loaded" in result and "secret" not in result
    assert drive_calls["search"] == ["latch"]  # Drive is still allowed afterwards
    assert "secret" not in caplog.text


def test_system_prompt_makes_decisions_outrank_drive_and_demands_both_links():
    prompt = llm.system_prompt()

    assert "find_decisions" in prompt
    assert "outranks every Drive document" in prompt
    assert "link both" in prompt and "possibly stale" in prompt
    assert "Only confirmed decisions bind" in prompt


# --- drafting a decision ---

TRANSCRIPT = [
    {"role": "user", "content": "Should we use a toggle or a cam latch?"},
    {"role": "assistant", "content": f"The notes {DOC_LINK} favor a cam latch."},
    {"role": "user", "content": "OK, cam latch. Toggles loosen with vibration."},
]
DRAFT = {
    "subsystem": "Enclosure",
    "topic": "Latch style",
    "decision": "Use the cam latch.",
    "rationale": "It holds under vibration.",
    "alternatives": [{"option": "Toggle", "rejected_because": "Loosens."}],
    "sources": [
        {"title": "Latch design notes", "link": DOC_LINK},
        {"title": "Invented", "link": "https://docs.google.com/document/d/nope1234567/edit"},
        {"title": "No link", "link": ""},
    ],
}


def test_draft_decision_returns_the_draft_and_drops_invented_sources(monkeypatch):
    client = use_client(monkeypatch, [fake.reply(json.dumps(DRAFT))])
    records = []

    draft = llm.draft_decision(TASK, TRANSCRIPT, record=records.append)

    assert draft["topic"] == "Latch style"
    assert [s["link"] for s in draft["sources"]] == [DOC_LINK]
    assert [r.status for r in records] == ["ok"]
    request = client.requests[0]
    assert "tools" not in request  # a draft never calls tools
    assert "Never invent a link" in request["messages"][0]["content"]
    assert "cam latch" in request["messages"][1]["content"]


def test_draft_decision_accepts_json_wrapped_in_a_code_fence(monkeypatch):
    use_client(monkeypatch, [fake.reply("```json\n" + json.dumps(DRAFT) + "\n```")])

    assert llm.draft_decision(TASK, TRANSCRIPT)["decision"] == "Use the cam latch."


@pytest.mark.parametrize("reply", ["I think you chose the cam latch.", "[1, 2]", "{broken"])
def test_draft_decision_that_is_not_json_is_a_plain_error(monkeypatch, reply):
    use_client(monkeypatch, [fake.reply(reply)])

    with pytest.raises(llm.LLMError) as error:
        llm.draft_decision(TASK, TRANSCRIPT)

    assert "couldn't turn this conversation into a draft" in error.value.message


def test_draft_decision_model_failure_is_logged_and_raised(monkeypatch):
    use_client(monkeypatch, [fake.timed_out()])
    records = []

    with pytest.raises(llm.LLMError) as error:
        llm.draft_decision(TASK, TRANSCRIPT, record=records.append)

    assert error.value.kind == "timeout"
    assert [r.status for r in records] == ["timeout"]
