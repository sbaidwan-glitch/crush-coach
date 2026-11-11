"""Scoring, summary and run-control for `make eval`. Made-up answers; no network."""

import json
from pathlib import Path

import pytest

import coach_eval
from coach_eval import (
    FAIL,
    NOT_RUN,
    PARTIAL,
    PASS,
    Outcome,
    Question,
    QuestionFileError,
    parse_questions,
    score_answer,
    summarize,
)


def run_questions(*args, **kwargs):
    """The real runner, but never actually waiting: tests must be instant."""
    kwargs.setdefault("sleep", lambda _: None)
    return coach_eval.run_questions(*args, **kwargs)


def q(**overrides) -> Question:
    base = {
        "id": "q1",
        "task": "M01",
        "question": "Which widget?",
        "kind": "lookup",
        "expect": ("alpha", "beta"),
        "cite": "",
        "must_not": (),
    }
    return Question(**{**base, **overrides})


# --- scoring ---


def test_pass_when_everything_expected_is_there():
    assert score_answer(q(), "Use alpha with beta.").score == PASS


def test_matching_ignores_case_and_line_wrapping():
    question = q(expect=("Side Panel",))
    assert score_answer(question, "the side\n  panel router").score == PASS
    assert score_answer(question, "the SIDE PANEL router").score == PASS


def test_curly_quotes_match_straight_quotes():
    assert score_answer(q(expect=("don't",)), "We don’t know.").score == PASS


def test_one_of_two_expected_is_partial():
    result = score_answer(q(), "Only alpha is mentioned.")
    assert result.score == PARTIAL
    assert result.missing == ["beta"]


def test_less_than_half_is_fail():
    question = q(expect=("a1", "b2", "c3"))
    assert score_answer(question, "a1 only").score == FAIL
    assert score_answer(question, "a1 and b2").score == PARTIAL


def test_nothing_expected_found_is_fail():
    assert score_answer(q(), "Nothing relevant.").score == FAIL


def test_must_not_fails_even_when_everything_expected_is_there():
    result = score_answer(q(must_not=("which season",)), "alpha beta. Which season?")
    assert result.score == FAIL
    assert result.forbidden == ["which season"]


def test_must_not_blocks_partial_too():
    assert score_answer(q(must_not=("oops",)), "alpha oops").score == FAIL


def test_cite_needs_a_linked_source_title():
    question = q(cite="Draw Latch")
    linked = ["6082A12_Draw Latch.pdf"]
    assert score_answer(question, "alpha beta", linked).score == PASS
    assert score_answer(question, "alpha beta", ["Other doc"]).cite_ok is False
    assert score_answer(question, "alpha beta", []).score == PARTIAL


def test_cite_match_is_case_insensitive():
    assert (
        score_answer(q(cite="draw latch"), "alpha beta", ["DRAW LATCH"]).score == PASS
    )


def test_cite_in_answer_text_alone_does_not_count():
    # Mentioning the file name is not the same as linking it.
    question = q(cite="Draw Latch")
    assert score_answer(question, "alpha beta, see Draw Latch").score == PARTIAL


def test_empty_cite_is_not_checked():
    result = score_answer(q(cite=""), "alpha beta")
    assert result.score == PASS
    assert result.cite_ok is None


def test_no_expectations_passes_unless_forbidden():
    assert score_answer(q(expect=()), "anything").score == PASS
    assert score_answer(q(expect=(), must_not=("bad",)), "bad").score == FAIL


def test_partial_needs_exactly_half_or_more():
    assert score_answer(q(expect=("a1", "b2", "c3", "d4")), "a1 b2").score == PARTIAL


def test_a_conflict_answer_must_name_both_values():
    question = q(expect=(".030", ".050"), must_not=("the clearance is .030 in.",))
    flagged = "Sources disagree: 0.030 in one doc, 0.050 in another."
    assert score_answer(question, flagged).score == PASS
    silent = "The clearance is .030 in."
    assert score_answer(question, silent).score == FAIL


# --- alternatives in `expect` ---


def test_an_expect_item_with_alternatives_matches_any_of_them():
    question = q(expect=(("not decided", "isn't recorded", "no decision"),))
    assert score_answer(question, "The lubricant isn\u2019t recorded.").score == PASS
    assert score_answer(question, "There was no decision yet.").score == PASS
    assert score_answer(question, "Use silicone grease.").score == FAIL


def test_alternatives_count_as_one_item_for_partial():
    question = q(expect=("5.75", ("verify", "confirm", "check with")))
    assert score_answer(question, "It is 5.75 in. Confirm with a lead.").score == PASS
    result = score_answer(question, "It is 5.75 in.")
    assert result.score == PARTIAL
    assert result.missing == ["verify / confirm / check with"]


def test_reason_keeps_the_original_case_of_what_is_missing():
    result = score_answer(q(expect=("6082A12",)), "no part number")
    assert "'6082A12'" in result.reason
    assert result.reason.startswith("Missing")


# --- scoring an outcome ---


def test_an_error_is_a_fail_even_if_text_would_pass():
    outcome = Outcome(answer="alpha beta", error="timed out", error_kind="timeout")
    result = coach_eval.score_outcome(q(), outcome)
    assert result.score == FAIL
    assert "timed out" in result.reason


def test_outcome_sources_feed_the_cite_check():
    outcome = Outcome(
        answer="alpha beta", sources=[{"title": "Seal Notes", "link": "x"}]
    )
    assert coach_eval.score_outcome(q(cite="seal"), outcome).score == PASS


# --- summary ---


def result_for(score_text: str, seconds: float, cost: float, **kw):
    outcome = Outcome(answer=score_text, seconds=seconds, cost=cost, **kw)
    question = q(expect=("ok",))
    result = coach_eval.score_outcome(question, outcome)
    return result


def test_summary_counts_median_max_and_cost():
    results = [
        result_for("ok", 10, 0.01),
        result_for("nope", 30, 0.02),
        result_for("ok", 20, 0.03),
    ]
    summary = summarize(results)
    assert (summary.passed, summary.partial, summary.failed) == (2, 0, 1)
    assert summary.median_seconds == 20
    assert summary.max_seconds == 30
    assert summary.total_cost == pytest.approx(0.06)
    assert summary.line() == (
        "pass 2 / partial 0 / fail 1 | median 20.0s, max 30.0s | total cost $0.0600"
    )


def test_summary_counts_errors_separately_from_fails():
    results = [
        result_for("ok", 1, 0.0),
        result_for("nope", 1, 0.0),
        result_for("", 1, 0.0, error="busy", error_kind="rate_limited", attempts=2),
    ]
    summary = summarize(results)
    assert (summary.failed, summary.errors, summary.retried) == (2, 1, 1)
    assert summary.line().startswith("pass 1 / partial 0 / fail 2 (1 error) |")


def test_summary_of_nothing_does_not_crash():
    assert summarize([]).line().startswith("pass 0 / partial 0 / fail 0")


def test_summary_flags_unreported_cost():
    results = [result_for("ok", 1, 0.0, cost_reported=False)]
    assert "no cost" in summarize(results).line()


# --- running ---


def questions(n: int) -> list[Question]:
    return [q(id=f"q{i}", expect=("ok",)) for i in range(n)]


def test_run_stops_when_total_cost_passes_the_limit():
    asked = []

    def ask(question):
        asked.append(question.id)
        return Outcome(answer="ok", cost=0.15)

    run = run_questions(questions(5), ask, max_cost=0.40)
    # 0.15, 0.30, 0.45 -> over the limit after the third question.
    assert asked == ["q0", "q1", "q2"]
    assert [r.score for r in run.results] == [PASS, PASS, PASS, NOT_RUN, NOT_RUN]
    assert "$0.45" in run.stopped and "$0.40" in run.stopped
    assert summarize(run.results).not_run == 2


def test_run_does_not_stop_at_exactly_the_limit():
    run = run_questions(questions(2), lambda _: Outcome(answer="ok", cost=0.20), 0.40)
    assert run.stopped is None
    assert [r.score for r in run.results] == [PASS, PASS]


def test_run_stops_when_the_glassroom_budget_is_used_up():
    def ask(question):
        return Outcome(error="budget", error_kind="budget_exceeded")

    run = run_questions(questions(3), ask)
    assert [r.score for r in run.results] == [FAIL, NOT_RUN, NOT_RUN]
    assert "budget" in run.stopped


def test_one_failed_question_does_not_stop_the_run():
    def ask(question):
        if question.id == "q0":
            return Outcome(error="slow", error_kind="timeout")
        return Outcome(answer="ok")

    run = run_questions(questions(3), ask)
    assert [r.score for r in run.results] == [FAIL, PASS, PASS]
    assert run.stopped is None


def flaky(failures: dict[str, list[Outcome]], default: Outcome | None = None):
    """An `ask` that returns the queued outcomes for a question id, then `default`."""
    calls: list[str] = []

    def ask(question):
        calls.append(question.id)
        queue = failures.get(question.id, [])
        if queue:
            return queue.pop(0)
        return default or Outcome(answer="ok")

    ask.calls = calls
    return ask


def no_sleep():
    slept: list[float] = []
    return slept, slept.append


def test_pauses_between_questions_but_not_before_the_first_or_after_the_last():
    slept, sleep = no_sleep()
    run_questions(questions(3), lambda _: Outcome(answer="ok"), pause=3, sleep=sleep)
    assert slept == [3, 3]


def test_a_failed_question_is_retried_once_and_the_retry_can_pass():
    slept, sleep = no_sleep()
    ask = flaky({"q0": [Outcome(error="busy", error_kind="rate_limited", cost=0.01)]})
    run = run_questions(
        questions(2), ask, pause=3, retry_pause=20, sleep=sleep, max_cost=1
    )
    assert ask.calls == ["q0", "q0", "q1"]
    assert slept == [20, 3]  # retry pause, then the pause between questions
    first = run.results[0]
    assert first.score == PASS
    assert first.outcome.attempts == 2
    assert "Retried once after: busy" in first.outcome.notes[-1]


def test_the_retry_cost_time_and_calls_are_counted():
    bad = Outcome(
        error="x",
        error_kind="error",
        cost=0.01,
        seconds=5,
        model_calls=2,
        input_tokens=100,
        output_tokens=10,
        drive_calls=None,
    )
    good = Outcome(
        answer="ok",
        cost=0.02,
        seconds=7,
        model_calls=3,
        input_tokens=200,
        output_tokens=20,
        drive_calls=4,
    )
    outcomes = iter([bad, good])
    run = run_questions(
        [q(expect=("ok",))], lambda _: next(outcomes), sleep=lambda _: None
    )
    o = run.results[0].outcome
    assert o.cost == pytest.approx(0.03)
    assert (o.seconds, o.model_calls) == (12, 5)
    assert (o.input_tokens, o.output_tokens) == (300, 30)
    assert o.drive_calls == 4
    assert summarize(run.results).total_cost == pytest.approx(0.03)


def test_there_is_at_most_one_retry():
    ask = flaky({}, default=Outcome(error="down", error_kind="timeout"))
    run = run_questions([q()], ask, sleep=lambda _: None)
    assert ask.calls == ["q1", "q1"]
    assert run.results[0].score == FAIL
    assert run.results[0].outcome.attempts == 2


def test_retry_cost_counts_toward_the_stop_limit():
    outcomes = iter(
        [
            Outcome(error="x", error_kind="error", cost=0.25),
            Outcome(answer="ok", cost=0.25),
        ]
    )
    run = run_questions(
        questions(3), lambda _: next(outcomes), max_cost=0.40, sleep=lambda _: None
    )
    # 0.25 + 0.25 = 0.50 passes 0.40 after the first question's retry.
    assert [r.score for r in run.results] == [PASS, NOT_RUN, NOT_RUN]
    assert "$0.5000" in run.stopped


def test_no_retry_when_it_would_start_past_the_limit():
    ask = flaky({}, default=Outcome(error="x", error_kind="error", cost=0.5))
    run = run_questions(questions(2), ask, max_cost=0.40, sleep=lambda _: None)
    assert ask.calls == ["q0"]
    assert run.results[0].outcome.attempts == 1
    assert run.results[1].score == NOT_RUN


def test_no_retry_when_the_budget_is_used_up():
    ask = flaky({}, default=Outcome(error="b", error_kind="budget_exceeded"))
    run_questions(questions(2), ask, sleep=lambda _: None)
    assert ask.calls == ["q0"]


def test_a_wrong_answer_is_not_retried():
    ask = flaky({}, default=Outcome(answer="wrong"))
    run_questions(questions(1), ask, sleep=lambda _: None)
    assert ask.calls == ["q0"]


# --- questions file ---


def entry(**overrides):
    return {"id": "a", "task": "M01", "question": "?", "kind": "lookup", **overrides}


def test_parse_accepts_missing_optional_fields():
    [parsed] = parse_questions([entry()])
    assert parsed.expect == () and parsed.cite == "" and parsed.must_not == ()


def test_parse_accepts_alternatives_in_expect():
    [parsed] = parse_questions([entry(expect=["5.75", ["verify", "confirm"]])])
    assert parsed.expect == ("5.75", ("verify", "confirm"))


def test_parse_rejects_bad_alternatives():
    for bad in ([[]], [["ok", 3]], [["ok", ""]]):
        with pytest.raises(QuestionFileError):
            parse_questions([entry(expect=bad)])


def test_parse_rejects_bad_files():
    for bad in (
        None,
        [],
        ["text"],
        [entry(kind="other")],
        [entry(id="")],
        [entry(), entry()],  # duplicate id
        [entry(expect=[0.03])],  # unquoted number
        [entry(expect="alpha")],  # not a list
    ):
        with pytest.raises(QuestionFileError):
            parse_questions(bad)


def test_select_filters_and_rejects_unknown_values():
    all_q = [q(id="a", kind="lookup"), q(id="b", kind="conflict")]
    assert [x.id for x in coach_eval.select_questions(all_q, only="b")] == ["b"]
    assert [x.id for x in coach_eval.select_questions(all_q, kind="lookup")] == ["a"]
    with pytest.raises(QuestionFileError):
        coach_eval.select_questions(all_q, only="nope")
    with pytest.raises(QuestionFileError):
        coach_eval.select_questions(all_q, kind="nope")
    with pytest.raises(QuestionFileError):
        coach_eval.select_questions(all_q, only="a", kind="conflict")


def test_the_real_questions_file_is_valid():
    path = Path(__file__).resolve().parents[2] / "evals" / "coach_questions.yaml"
    loaded = coach_eval.load_questions(path)
    assert len(loaded) >= 15
    assert len({x.id for x in loaded}) == len(loaded)
    by_id = {x.id: x for x in loaded}
    assert any(isinstance(item, tuple) for item in by_id["oring-lubricant"].expect)


# --- reports ---


def test_reports_contain_every_answer(tmp_path):
    outcome = Outcome(
        answer="Use alpha.\n\nAnd beta.", seconds=2.5, cost=0.01, model_calls=2
    )
    run = run_questions([q()], lambda _: outcome)
    meta = {
        "started_at": "2026-11-09 10:00:00",
        "model": "test-model",
        "git": "abc123",
        "filters": "all",
    }
    json_path, md_path = coach_eval.write_reports(
        tmp_path, "20261109-100000", meta, run
    )

    data = json.loads(json_path.read_text())
    assert data["summary"]["passed"] == 1
    assert data["results"][0]["outcome"]["answer"] == "Use alpha.\n\nAnd beta."
    md = md_path.read_text()
    assert "> Use alpha." in md and "> And beta." in md
    assert "pass 1 / partial 0 / fail 0" in md
