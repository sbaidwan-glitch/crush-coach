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
