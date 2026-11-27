"""Plan my next steps: the button under the latest answer, one short streamed call over the
same retrieval, the fixed shape of the answer, its prompt box with Copy and a warning, and the
"Then" taken from the task's steps. Made-up files; the model is a script."""

import json

import fake_openai as fake
import pytest
from fastapi.testclient import TestClient
from index_helpers import add_file
from sqlmodel import select

import auth
import coach
import llm
import main
import plan
import steps
import taskpage
from models import CoachMessage, LlmCall, Task

LINK = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"
DOC_ID = "1AbCdEfGhIjKlMnOp"
SAM = {"name": "Sam Example", "email": "sam@example.edu", "role": "member"}

PROMPT = (
    "I am making the enclosure lid for a small underwater robot. I already know the lid latch is a "
    "two-point latch. What do published guides say about latch seating and squeeze? Name your sources."
)
PLAN = f"""**What I found**
- The two-point latch holds the lid shut ([Latch design notes]({LINK})).

**What's missing**
- Nothing says how the latch is seated.

**Do this now**
Open the latch notes and write down which faces the latch touches.

**If you need more help**
```
{PROMPT}
```

**You should end up with**
- A short note listing which faces the latch touches, with a link to the notes.
- A sketch of the lid and latch, labelled."""


@pytest.fixture(autouse=True)
def no_real_services(monkeypatch):
    monkeypatch.delenv("COACH_DAILY_LIMIT", raising=False)
    monkeypatch.delenv("COACH_MODEL", raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("tried to use a real service")

    monkeypatch.setattr(llm, "get_client", refuse)


@pytest.fixture
def client():
    main.app.dependency_overrides[auth.require_user] = lambda: SAM
    main.app.dependency_overrides[auth.optional_user] = lambda: SAM
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def task(session):
    session.add(
        Task(code="M04", title="Finish enclosure wall panels", subsystem="Enclosure",
             task_type="Fabricate", stage="Drawing", status="in_progress", owner="Sam Example")
    )
    session.commit()
    add_file(session, "Latch design notes", folder="Crush / Enclosure", modified="2026-09-30",
             chunks=["Use the two-point latch on the lid. The latch holds the lid shut."],
             drive_id=DOC_ID, link=LINK)
    return session.exec(select(Task)).one()


def script(monkeypatch, *items):
    model = fake.FakeClient(items)
    monkeypatch.setattr(llm, "get_client", lambda: model)
    return model


def stream(client, text="How should the latch work?", **data):
    response = client.post("/tasks/M04/coach/stream", data={"message": text, **data})
    assert response.status_code == 200
    return [json.loads(line) for line in response.text.splitlines() if line]


def stored(session, model):
    session.expire_all()
    return list(session.exec(select(model)).all())


# --- the shape of a plan (pure) -----------------------------------------------------------------------


def test_a_plan_splits_into_its_labelled_parts_and_the_prompt_box():
    parts = plan.parse(PLAN + "\n\n**Then:** Lead review comes after Drawing.")

    assert [p["key"] for p in parts] == ["found", "missing", "now", "help", "outputs", "then"]
    by = {p["key"]: p for p in parts}
    assert "two-point latch" in by["found"]["text"] and "faces" in by["now"]["text"]
    assert by["help"]["prompt"] == PROMPT and by["help"]["text"] == ""
    assert by["then"]["text"] == "Lead review comes after Drawing."


@pytest.mark.parametrize(
    "line",
    ["**What I found**", "**What I found:**", "## What I found", "What I found:", "**What I found** - one", "**WHAT I FOUND**", "**What I found**:"],
)
def test_the_labels_are_found_however_they_are_dressed(line):
    assert plan.heading(line) and plan.heading(line)[0] == "found"


def test_a_curly_apostrophe_is_still_the_whats_missing_label():
    assert plan.heading("**What’s missing**")[0] == "missing"


@pytest.mark.parametrize("line", ["Then add the gasket.", "Then, add the gasket", "This is what I found", "- **Do this now** later"])
def test_ordinary_sentences_are_not_labels(line):
    assert plan.heading(line) is None


def test_an_answer_with_too_few_labels_is_not_a_plan():
    assert plan.parse("**What I found**\nA thing.\n\n**Do this now**\nDo it.") == []
    assert plan.parse("") == [] and plan.parse("Just a normal answer.\n\nNext step: ask.") == []


def test_an_unclosed_fence_still_gives_a_prompt():
    cut = PLAN.split("```\n")[0] + "```\nI am making the lid and what do guides say about"

    by = {p["key"]: p for p in plan.parse(cut)}

    assert by["help"]["prompt"] == "I am making the lid and what do guides say about"


def test_without_a_fence_the_whole_part_is_the_prompt():
    text = PLAN.replace("```\n", "").replace("\n```", "")

    by = {p["key"]: p for p in plan.parse(text)}

    assert by["help"]["prompt"].startswith("I am making the enclosure lid")


def test_numbers_in_you_should_end_up_with_are_found_but_links_do_not_count():
    assert plan.has_numbers_in_outputs(PLAN) is False
    assert plan.has_numbers_in_outputs(PLAN.replace("labelled.", "labelled at 6 mm.")) is True
    with_link = PLAN.replace("with a link to the notes", "with a link to [the notes](https://docs.google.com/document/d/1AbCdEf123/edit)")
    assert plan.has_numbers_in_outputs(with_link) is False


def test_do_this_now_is_read_as_plain_text():
    assert plan.do_this_now(PLAN) == "Open the latch notes and write down which faces the latch touches."
    assert taskpage.next_step_from("no") is None


# --- "Then" comes from the steps, with no model ------------------------------------------------------------


def make(task_type="Fabricate", stage="Drawing", status="in_progress"):
    return Task(code="X", title="t", subsystem="Enclosure", task_type=task_type, stage=stage, status=status)


def test_then_names_the_next_step_after_the_current_one():
    assert steps.then_text(make(stage="Drawing", status="in_progress")) == (
        "Lead review comes after Drawing: ask for review, and a lead checks the work before it moves on."
    )
    assert steps.then_text(make("Print", "Check CAD")) == (
        "Lead review comes after Check CAD: ask for review, and a lead checks the work before it moves on."
    )
    assert steps.then_text(make("Fabricate", "Process router", "in_progress")) == "Shop is the next step after Process router."


def test_then_on_the_last_step_and_when_finished():
    assert steps.then_text(make("Fabricate", "Inspect", "in_progress")) == "Inspect is the last step. When it is done, the task is finished."
    assert steps.then_text(make(status="done")) == "Every step of this task is done."


def test_then_follows_a_leads_own_steps():
    task = make()
    task.steps = [{"name": "Sketch", "review": False}, {"name": "Weld", "review": False}]
    task.stage = "Sketch"

    assert steps.then_text(task) == "Weld is the next step after Sketch."


def test_the_prompt_file_has_the_shape_and_the_250_token_limit():
    prompt = llm.plan_prompt()
    for label in ("What I found", "What's missing", "Do this now", "If you need more help", "You should end up with"):
        assert f"**{label}**" in prompt
    assert "250 tokens" in prompt and "No numbers in this part" in prompt and "You have no tools" in prompt


# --- the button -----------------------------------------------------------------------------------------------


def test_the_button_is_under_the_latest_answer_only(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("The latch holds."), fake.reply("Second answer."))
    stream(client, "How should the latch work?")
    stream(client, "And the hinge?")

    page = client.get("/tasks/M04").text

    assert page.count("data-coach-plan") == 1
    assert page.rindex("data-coach-plan") > page.index("Second answer.")
    assert 'name="plan" value="1"' in page and "Plan my next steps" in page


def test_no_button_before_any_answer_or_under_a_failure(monkeypatch, session, task, client):
    assert "data-coach-plan" not in client.get("/tasks/M04").text
    script(monkeypatch, fake.rate_limited())
    stream(client)

    assert "data-coach-plan" not in client.get("/tasks/M04").text


def test_a_fresh_answer_brings_the_button_and_a_plan_does_not_offer_either_button(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("The latch holds."), fake.reply(PLAN))

    first = stream(client)
    second = stream(client, "", plan="1")

    assert "data-coach-plan" in first[-1]["html"] and "data-coach-recommend" in first[-1]["html"]
    assert second[-1]["type"] == "done"
    assert "data-coach-plan" not in second[-1]["html"] and "data-coach-recommend" not in second[-1]["html"]


# --- pressing it -------------------------------------------------------------------------------------------------


def test_pressing_it_makes_one_short_call_with_no_tools_over_the_same_retrieval(monkeypatch, session, task, client):
    model = script(monkeypatch, fake.reply(f"The latch holds ([notes]({LINK}))."), fake.reply(PLAN, input_tokens=2500, output_tokens=240))
    stream(client, "How should the latch work?")

    events = stream(client, "", plan="1")

    assert len(model.requests) == 2  # the first answer, then exactly one call for the plan
    request = model.requests[1]
    assert "tools" not in request and "tool_choice" not in request  # no tools, so it can't wander
    assert request["max_tokens"] == llm.PLAN_MAX_TOKENS == 1000
    system = request["messages"][0]["content"]
    assert "Plan mode" in system and "under about 250 tokens" in system
    prompt = request["messages"][-1]["content"]
    assert "Plan my next steps." in prompt and "Latch design notes" in prompt  # retrieval for the design question
    assert "How should the latch work?" in [m["content"] for m in request["messages"] if m["role"] == "user"]
    steps_seen = [e["text"] for e in events if e["type"] == "step"]
    assert any("Planning" in text for text in steps_seen)
    done = events[-1]
    assert done["type"] == "done"
    assert done["html"].count('class="plan"') == 1


def test_the_saved_plan_ends_with_then_from_the_steps_and_is_marked_as_a_plan(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("The latch holds."), fake.reply(PLAN))
    stream(client)

    stream(client, "", plan="1")

    saved = sorted(stored(session, CoachMessage), key=lambda m: m.id)
    assert [m.role for m in saved] == ["user", "assistant", "user", "assistant"]
    assert saved[2].content == coach.PLAN_REQUEST == "Plan my next steps."
    assert json.loads(saved[3].meta)["plan"] is True
    assert saved[3].content.rstrip().endswith(
        "**Then:** Lead review comes after Drawing: ask for review, and a lead checks the work before it moves on."
    )
    assert [p["key"] for p in plan.parse(saved[3].content)][-1] == "then"


def test_the_page_shows_each_part_the_prompt_in_a_box_and_the_warning_beside_copy(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("The latch holds."), fake.reply(PLAN))
    stream(client)
    stream(client, "", plan="1")

    html = client.get("/tasks/M04").text
    page = html[html.index('class="plan"'):]

    for title in ("What I found", "What&#39;s missing", "What's missing", "Do this now", "If you need more help", "You should end up with", "Then"):
        if title in page:
            break
    else:
        pytest.fail("no part titles")
    for title in ("What I found", "Do this now", "If you need more help", "You should end up with", "Then"):
        assert f'plan__title">{title}</h3>' in page
    assert '<pre class="promptbox__text"' in page and "I am making the enclosure lid" in page
    box = page[page.index('class="promptbox"'):]
    assert 'data-copy-target="plan-prompt-' in box and ">Copy</button>" in box.split("</div>")[0]
    # The warning sits in the same bar as the Copy button.
    bar = box[box.index('class="promptbox__bar"'):box.index("</section>")]
    assert "This text includes information from the team Drive. Paste it only into tools your team allows." in bar
    assert "hidden" in bar.split(">Copy<")[0]  # shown by JavaScript only, since it needs it
    assert "Lead review comes after Drawing" in page
    assert f'href="{LINK}"' in page  # sources stay links; the list of sources is under the plan
    assert "Ask a lead to settle this" not in html.split('class="plan"')[1]  # nothing to settle in a plan


def test_a_plan_streams_as_ordinary_text_then_arrives_whole(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("The latch holds."), fake.reply(PLAN))
    stream(client)

    events = stream(client, "", plan="1")

    texts = [e for e in events if e["type"] == "text"]
    assert texts and "What I found" in texts[-1]["html"]  # the partial answer is plain Markdown
    assert events[-1]["type"] == "done" and "promptbox" in events[-1]["html"]


def test_a_cut_off_plan_is_shortened_and_still_a_plan(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("The latch holds."), fake.reply(PLAN + "\n- And then a very long thought that", finish_reason="length"))
    stream(client)

    events = stream(client, "", plan="1")

    html = events[-1]["html"]
    assert "shortened" in html and 'class="plan"' in html


def test_a_plan_that_does_not_follow_the_shape_is_shown_as_an_ordinary_answer(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("The latch holds."), fake.reply("Just read the latch notes.\n\nNext step: open them."))
    stream(client)

    events = stream(client, "", plan="1")

    assert 'class="plan"' not in events[-1]["html"] and "Just read the latch notes." in events[-1]["html"]


def test_numbers_in_you_should_end_up_with_get_a_note(monkeypatch, session, task, client):
    bad = PLAN.replace("labelled.", "labelled at 6 mm.")
    script(monkeypatch, fake.reply("The latch holds."), fake.reply(bad))
    stream(client)

    events = stream(client, "", plan="1")

    assert "has numbers in it, and it shouldn" in events[-1]["html"].replace("&#39;", "'")


def test_the_call_is_logged_as_a_plan_with_its_cost(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("The latch holds."), fake.reply(PLAN, input_tokens=2500, output_tokens=240))
    stream(client)

    stream(client, "", plan="1")

    calls = sorted(stored(session, LlmCall), key=lambda c: c.id)
    assert [c.purpose for c in calls] == ["coach", "plan"]
    assert (calls[1].input_tokens, calls[1].output_tokens, calls[1].cost) == (2500, 240, 0.0012)


def test_it_is_rejected_when_nothing_has_been_asked_yet(monkeypatch, session, task, client):
    script(monkeypatch)  # a model call would raise

    events = stream(client, "", plan="1")

    assert events[-1]["type"] == "rejected" and "Ask the coach about this task first" in events[-1]["message"]
    assert stored(session, CoachMessage) == [] and stored(session, LlmCall) == []


def test_it_counts_toward_the_daily_cap(monkeypatch, session, task, client):
    monkeypatch.setenv("COACH_DAILY_LIMIT", "2")
    script(monkeypatch, fake.reply("The latch holds."), fake.reply(PLAN))
    stream(client)
    stream(client, "", plan="1")

    events = stream(client, "And one more?")

    assert events[-1]["type"] == "rejected" and "2 coach messages" in events[-1]["message"]


def test_retrying_a_failed_plan_is_a_plan_again(monkeypatch, session, task, client):
    model = script(monkeypatch, fake.reply("The latch holds."), fake.rate_limited(), fake.reply(PLAN))
    stream(client)
    failed = stream(client, "", plan="1")
    assert failed[-1]["type"] == "failed"
    [error] = [m for m in stored(session, CoachMessage) if m.role == "error"]

    events = stream(client, "", retry=str(json.loads(error.meta)["retry_of"]))

    assert events[-1]["type"] == "done" and 'class="plan"' in events[-1]["html"]
    assert "Plan mode" in model.requests[-1]["messages"][0]["content"]
    assert [c.purpose for c in sorted(stored(session, LlmCall), key=lambda c: c.id)] == ["coach", "plan", "plan"]


def test_the_plain_form_post_plans_too(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("The latch holds."), fake.reply(PLAN))
    stream(client)

    response = client.post("/tasks/M04/coach", data={"plan": "1"}, headers={"HX-Request": "true"})

    assert 'class="plan"' in response.text and "Do this now" in response.text


def test_the_plan_is_never_made_by_opening_a_page(monkeypatch, session, task, client):
    model = script(monkeypatch)

    for path in ("/tasks/M04", "/", "/tasks"):
        client.get(path)

    assert model.requests == []


def test_home_shows_do_this_now_as_the_next_step_after_a_plan(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("The latch holds.\n\nNext step: ask a lead."), fake.reply(PLAN))
    monkeypatch.setattr(auth, "session_user", lambda request: SAM)  # Home reads the session itself
    stream(client)
    stream(client, "", plan="1")

    home = client.get("/").text

    assert "Open the latch notes and write down which faces the latch touches." in home
    assert "ask a lead" not in home  # only the newest answer counts


def test_the_plan_is_in_the_message_history_the_next_question_sees(monkeypatch, session, task, client):
    model = script(monkeypatch, fake.reply("The latch holds."), fake.reply(PLAN), fake.reply("Yes."))
    stream(client)
    stream(client, "", plan="1")

    stream(client, "Which faces?")

    roles = [(m["role"], m["content"][:20]) for m in model.requests[2]["messages"] if m["role"] != "system"]
    assert ("user", "Plan my next steps.") in roles


def test_a_database_failure_after_the_plan_still_shows_the_plan(monkeypatch, session, task, client):
    script(monkeypatch, fake.reply("The latch holds."), fake.reply(PLAN))
    stream(client)
    real = coach._add_llm_calls

    def broken(*args, **kwargs):
        raise RuntimeError("password=hunter2")

    monkeypatch.setattr(coach, "_add_llm_calls", broken)
    events = stream(client, "", plan="1")
    monkeypatch.setattr(coach, "_add_llm_calls", real)

    assert events[-1]["type"] == "done" and "hunter2" not in json.dumps(events)
    assert "could not be saved" in events[-1]["html"]
