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


def test_at_most_six_tool_calls_per_answer(monkeypatch, drive_calls):
    assert llm.MAX_TOOL_CALLS == 6
    client = use_client(
        monkeypatch,
        [
            fake.tool_request(search_call("a"), search_call("b")),
            fake.tool_request(*[search_call(f"c{i}") for i in range(5)]),
            fake.reply("Here is what I found."),
        ],
    )

    answer = llm.run_coach(TASK, [], "latch?")

    assert len(drive_calls["search"]) == 6  # the seventh request was refused
    assert answer.tool_calls == 6
    tool_results = [m for m in client.requests[2]["messages"] if m["role"] == "tool"]
    assert len(tool_results) == 7
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
            fake.tool_request(*[search_call(f"c{i}") for i in range(6)]),
            fake.tool_request(search_call("again")),
        ],
    )

    answer = llm.run_coach(TASK, [], "latch?")

    assert len(drive_calls["search"]) == 6
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


# --- file types, folders, progress steps ---

FOLDER_ID = "folderCAD1234567890abcdef"
FOLDER_LINK = f"https://drive.google.com/drive/folders/{FOLDER_ID}"
ASM_LINK = "https://drive.google.com/file/d/1ASMfileIdAbCdEfGhIj/view"


def list_call(call_id="c3", folder="Mechanical/CAD"):
    return (call_id, "drive_list_folder", json.dumps({"folder": folder}))


def typed_search_call(call_id="c1", query="enclosure", file_type="cad"):
    return (
        call_id,
        "drive_search",
        json.dumps({"query": query, "file_type": file_type}),
    )


def test_tools_offer_file_types_and_folder_listing(monkeypatch):
    client = use_client(monkeypatch, [fake.reply("Hi")])

    llm.run_coach(TASK, [], "hello")

    tools = {t["function"]["name"]: t["function"] for t in client.requests[0]["tools"]}
    assert set(tools) == {"drive_search", "drive_list_folder", "drive_read_text"}
    file_type = tools["drive_search"]["parameters"]["properties"]["file_type"]
    assert file_type["enum"] == ["cad", "sheets", "docs", "pdf"]
    assert "file_type" not in tools["drive_search"]["parameters"]["required"]
    assert tools["drive_list_folder"]["parameters"]["required"] == ["folder"]


def test_file_type_reaches_drive_and_plain_searches_do_not_send_one(monkeypatch):
    seen = []

    def fake_search(query, limit=10, file_type=None):
        seen.append((query, file_type))
        return [
            search_result(
                link=ASM_LINK,
                title="MdSb26_Crush_Enclosure.SLDASM",
                folder="Mechanical / CAD / Finalized",
            )
        ]

    monkeypatch.setattr(drive, "search", fake_search)
    use_client(
        monkeypatch,
        [
            fake.tool_request(typed_search_call(), search_call("c2", "latch")),
            fake.reply(f"The newest assembly is {ASM_LINK}"),
        ],
    )

    answer = llm.run_coach(TASK, [], "Where is the enclosure assembly CAD?")

    assert seen == [("enclosure", "cad"), ("latch", None)]
    assert [s["title"] for s in answer.sources] == ["MdSb26_Crush_Enclosure.SLDASM"]


def test_a_cad_search_may_have_an_empty_query_but_a_search_needs_something(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        drive,
        "search",
        lambda q, limit=10, file_type=None: calls.append((q, file_type)) or [],
    )
    client = use_client(
        monkeypatch,
        [
            fake.tool_request(
                typed_search_call("c1", query="", file_type="cad"),
                ("c2", "drive_search", json.dumps({"query": ""})),
            ),
            fake.reply("ok"),
        ],
    )

    llm.run_coach(TASK, [], "cad?")

    assert calls == [("", "cad")]
    results = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
    assert "needs a query or a file_type" in results[1]["content"]


def test_list_folder_tool_returns_entries_and_cites_listed_files(monkeypatch):
    monkeypatch.setattr(
        drive,
        "list_folder",
        lambda folder, limit=100: {
            "folder_path": "MuddSub / Mechanical / CAD",
            "folder_id": FOLDER_ID,
            "entries": [
                {
                    "id": "1ASMfileIdAbCdEfGhIj",
                    "name": "MdSb26_Crush_Enclosure.SLDASM",
                    "kind": "cad",
                    "modified_time": "2026-10-30T00:00:00.000Z",
                    "link": ASM_LINK,
                }
            ],
            "total": 1,
            "candidates": [],
        },
    )
    client = use_client(
        monkeypatch,
        [
            fake.tool_request(list_call()),
            fake.reply(f"It is in MuddSub / Mechanical / CAD: {ASM_LINK}"),
        ],
    )

    answer = llm.run_coach(TASK, [], "Where is the CAD?")

    result = json.loads(client.requests[1]["messages"][-1]["content"])
    assert result["folder_path"] == "MuddSub / Mechanical / CAD"
    assert result["entries"][0] == {
        "name": "MdSb26_Crush_Enclosure.SLDASM",
        "kind": "cad",
        "modified": "2026-10-30",
        "link": ASM_LINK,
        "id": "1ASMfileIdAbCdEfGhIj",
    }
    assert [s["link"] for s in answer.sources] == [ASM_LINK]
    assert not answer.drive_failed


def test_ambiguous_folder_path_asks_the_model_to_pick(monkeypatch):
    monkeypatch.setattr(
        drive,
        "list_folder",
        lambda folder, limit=100: {
            "folder_path": "",
            "folder_id": "",
            "entries": [],
            "total": 0,
            "candidates": [
                {
                    "id": FOLDER_ID,
                    "folder_path": "MuddSub / 2026 / CAD",
                    "modified_time": "2026-10-20T00:00:00.000Z",
                },
                {
                    "id": "folderOLDCAD1234567890ab",
                    "folder_path": "MuddSub / 2025 / CAD",
                    "modified_time": "2025-05-01T00:00:00.000Z",
                },
            ],
        },
    )
    client = use_client(
        monkeypatch, [fake.tool_request(list_call(folder="CAD")), fake.reply("ok")]
    )

    llm.run_coach(TASK, [], "CAD?")

    result = json.loads(client.requests[1]["messages"][-1]["content"])
    assert [c["folder_path"] for c in result["several_folders_match"]] == [
        "MuddSub / 2026 / CAD",
        "MuddSub / 2025 / CAD",
    ]
    assert "drive_list_folder again with its id" in result["instruction"]


def test_missing_folder_is_not_reported_as_a_drive_failure(monkeypatch):
    def not_found(folder, limit=100):
        raise drive.FolderNotFound("No folder matches 'Mechanical/Nope'.")

    monkeypatch.setattr(drive, "list_folder", not_found)
    client = use_client(
        monkeypatch,
        [fake.tool_request(list_call(folder="Mechanical/Nope")), fake.reply("ok")],
    )

    answer = llm.run_coach(TASK, [], "CAD?")

    assert answer.drive_failed is False
    result = json.loads(client.requests[1]["messages"][-1]["content"])
    assert "No folder matches" in result["error"]
    assert "Do not guess" in result["instruction"]


def test_progress_steps_are_reported_in_order(monkeypatch, drive_calls):
    monkeypatch.setattr(
        drive,
        "list_folder",
        lambda folder, limit=100: {
            "folder_path": "A / CAD",
            "folder_id": FOLDER_ID,
            "entries": [],
            "total": 0,
            "candidates": [],
        },
    )
    use_client(
        monkeypatch,
        [
            fake.tool_request(search_call("c1", "enclosure")),
            fake.tool_request(read_call("c2"), list_call("c3")),
            fake.reply("Done."),
        ],
    )
    steps = []

    llm.run_coach(TASK, [], "enclosure?", on_step=steps.append)

    assert steps == [
        "Searching 'enclosure'…",
        "Writing the answer…",
        "Reading Latch design notes…",
        "Looking in the folder Mechanical/CAD…",
        "Writing the answer…",
    ]


def test_a_broken_progress_callback_does_not_break_the_answer(monkeypatch, drive_calls):
    use_client(monkeypatch, [fake.tool_request(search_call()), fake.reply("Fine.")])

    def broken(step):
        raise RuntimeError("boom")

    answer = llm.run_coach(TASK, [], "latch?", on_step=broken)

    assert answer.text == "Fine."


# --- no unsupported claims ---


def test_empty_search_tells_the_model_what_was_tried_and_not_to_guess(monkeypatch):
    monkeypatch.setattr(drive, "search", lambda query, limit=10, file_type=None: [])
    client = use_client(
        monkeypatch,
        [
            fake.tool_request(search_call("c1", "tray layout")),
            fake.tool_request(typed_search_call("c2", "tray", "cad")),
            fake.reply("I found nothing."),
        ],
    )

    llm.run_coach(TASK, [], "Where is the tray layout?")

    second = json.loads(client.requests[2]["messages"][-1]["content"])
    assert second["results"] == []
    assert second["searches_tried"] == ["'tray layout'", "'tray' (cad files)"]
    assert "list the searches tried" in second["instruction"]
    assert "Do not guess where the file might be" in second["instruction"]


def test_nothing_found_and_an_invented_link_does_not_reach_the_member(monkeypatch):
    """The search finds nothing, then the model invents a location and a link."""
    monkeypatch.setattr(drive, "search", lambda query, limit=10, file_type=None: [])
    invented = "https://docs.google.com/document/d/1MadeUpDocIdThatNoToolReturned/edit"
    use_client(
        monkeypatch,
        [
            fake.tool_request(search_call("c1", "tray layout")),
            fake.reply(f"It is probably in Mechanical/CAD, see {invented}."),
        ],
    )

    answer = llm.run_coach(TASK, [], "Where is the tray layout?")

    assert invented not in answer.text
    assert "link removed" in answer.text
    assert answer.sources == []
    assert answer.ungrounded_links is True


def test_links_that_a_tool_returned_are_kept(monkeypatch, drive_calls):
    use_client(
        monkeypatch,
        [fake.tool_request(search_call()), fake.reply(f"See {DOC_LINK}.")],
    )

    answer = llm.run_coach(TASK, [], "latch?")

    assert DOC_LINK in answer.text
    assert answer.ungrounded_links is False


def test_the_same_document_under_another_link_shape_is_still_grounded(
    monkeypatch, drive_calls
):
    use_client(
        monkeypatch,
        [
            fake.tool_request(search_call()),
            fake.reply("See https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/view"),
        ],
    )

    answer = llm.run_coach(TASK, [], "latch?")

    assert answer.ungrounded_links is False


@pytest.mark.parametrize(
    "rule",
    [
        # 3. no unsupported claims
        "Never make claims about where files live, how the team works, or what tools the team uses",
        "unless a search result or a document you read shows it",
        "List the searches you tried",
        "who to ask",
        # 4. search before asking
        'Never ask "should I search for it?"',
        "Ask a clarifying question only after searching",
        # 5. CAD and spreadsheets
        "MdSb26_Crush_",
        ".SLDASM",
        "`file_type` set to `cad`",
        "`drive_list_folder`",
        '"Finalized"',
        "synonyms and part names",
        "electronics enclosure",
        "baseplate",
    ],
)
def test_the_system_prompt_carries_the_coaching_rules(rule):
    assert rule in llm.system_prompt()


def test_a_follow_up_may_cite_a_link_from_the_earlier_answer(monkeypatch):
    """Real check: "where is it?" made no search and its link was wrongly removed."""
    use_client(monkeypatch, [fake.reply(f"It is here: {DOC_LINK}")])
    history = [
        {"role": "user", "content": "How do I start?"},
        {"role": "assistant", "content": f"Open {DOC_LINK}."},
    ]

    answer = llm.run_coach(TASK, history, "Where is it?")

    assert DOC_LINK in answer.text
    assert answer.ungrounded_links is False


def test_a_follow_up_still_cannot_invent_a_link(monkeypatch):
    use_client(
        monkeypatch,
        [
            fake.reply(
                "See https://docs.google.com/document/d/1MadeUpDocIdThatNoToolReturned/edit"
            )
        ],
    )
    history = [{"role": "assistant", "content": f"Open {DOC_LINK}."}]

    answer = llm.run_coach(TASK, history, "Where is it?")

    assert "MadeUp" not in answer.text and answer.ungrounded_links is True


def test_a_folder_link_variant_is_grounded(monkeypatch):
    use_client(
        monkeypatch,
        [fake.reply(f"Browse {FOLDER_LINK}?usp=sharing")],
    )
    history = [{"role": "assistant", "content": f"The folder is {FOLDER_LINK}"}]

    answer = llm.run_coach(TASK, history, "Where?")

    assert answer.ungrounded_links is False


def test_the_prompt_says_to_quote_paths_exactly():
    assert "copy the folder path and file name exactly" in llm.system_prompt()
