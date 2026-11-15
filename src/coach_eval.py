"""Measure the coach: load the eval questions, score answers, summarize, write reports.

This module is pure logic with no network, database or model code, so it is fully
unit-tested. `scripts/run_evals.py` (`make eval`) supplies the function that really
asks the coach.

Scoring (case-insensitive substring matches on the answer text):
  pass     every `expect` appears, `cite` (if set) is the title of a source the answer
           links, and no `must_not` appears
  partial  at least half of `expect` appears and no `must_not` appears
  fail     anything else, including an error or timeout

An `expect` item is either one phrase or a list of alternatives (any one of them
counts), for answers that can be worded several ways: `["not decided", "no decision"]`.
"""

import json
import re
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

KINDS = ("lookup", "how-to", "why", "conflict", "unknown", "follow-up", "recommend")
DEFAULT_MAX_COST = 0.60  # dollars; the Glassroom cap is $1 per 5 hours
DEFAULT_PAUSE = 3.0  # seconds between questions, so the run doesn't hit rate limits
DEFAULT_RETRY_PAUSE = 20.0  # seconds before the one retry of a failed question
RETRYABLE = (
    "rate_limited",
    "timeout",
    "error",
)  # not budget_exceeded: retrying can't help

PASS, PARTIAL, FAIL = "pass", "partial", "fail"
NOT_RUN = "not run"  # skipped because the run stopped early


# --- questions ---


class QuestionFileError(ValueError):
    """The questions file is malformed. The message says which question and field."""


@dataclass(frozen=True)
class Question:
    id: str
    task: str
    question: str
    kind: str
    # Each item is one phrase, or a tuple of alternatives of which any one counts.
    expect: tuple[str | tuple[str, ...], ...] = ()
    cite: str = ""
    must_not: tuple[str, ...] = ()
    # The id of the question this one follows up: it is asked in the same chat, after
    # that question and its answer, the way a member would type it.
    after: str = ""
    # A recommendation: asked like pressing the Recommend button after `after`, with the
    # reference-book search and the calculator. `question` is the button's own text.
    recommend: bool = False


def _text_list(entry: dict, field_name: str, where: str) -> tuple[str, ...]:
    value = entry.get(field_name) or []
    if not isinstance(value, list):
        raise QuestionFileError(f"{where}: `{field_name}` must be a list")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise QuestionFileError(
                f"{where}: every `{field_name}` item must be non-empty text "
                f'(put quotes around numbers like ".030"), got {item!r}'
            )
    return tuple(value)


def _expect_list(entry: dict, where: str) -> tuple[str | tuple[str, ...], ...]:
    value = entry.get("expect") or []
    if not isinstance(value, list):
        raise QuestionFileError(f"{where}: `expect` must be a list")
    items: list[str | tuple[str, ...]] = []
    for item in value:
        if isinstance(item, list):
            _text_list({"expect": item}, "expect", where)
            if not item:
                raise QuestionFileError(
                    f"{where}: an `expect` alternatives list is empty"
                )
            items.append(tuple(item))
        else:
            items.append(_text_list({"expect": [item]}, "expect", where)[0])
    return tuple(items)


def parse_questions(data: object) -> list[Question]:
    """Validate the parsed YAML and return the questions in file order."""
    if not isinstance(data, list) or not data:
        raise QuestionFileError("the file must be a non-empty list of questions")
    questions: list[Question] = []
    seen: set[str] = set()
    seen_before: set[str] = set()  # ids above the current question
    for index, entry in enumerate(data, start=1):
        label = entry.get("id") if isinstance(entry, dict) else None
        where = f"question {label!r}" if label else f"question #{index}"
        if not isinstance(entry, dict):
            raise QuestionFileError(f"{where}: must be a mapping of fields")
        for required in ("id", "task", "question", "kind"):
            value = entry.get(required)
            if not isinstance(value, str) or not value.strip():
                raise QuestionFileError(f"{where}: `{required}` is missing or empty")
        if entry["id"] in seen:
            raise QuestionFileError(f"{where}: duplicate id")
        seen.add(entry["id"])
        if entry["kind"] not in KINDS:
            raise QuestionFileError(
                f"{where}: kind {entry['kind']!r} is not one of {', '.join(KINDS)}"
            )
        cite = entry.get("cite") or ""
        if not isinstance(cite, str):
            raise QuestionFileError(f"{where}: `cite` must be text")
        recommend = entry.get("recommend") or False
        if not isinstance(recommend, bool):
            raise QuestionFileError(f"{where}: `recommend` must be true or false")
        after = entry.get("after") or ""
        if not isinstance(after, str) or (after and after not in seen_before):
            raise QuestionFileError(
                f"{where}: `after` must be the id of a question earlier in the file"
            )
        if recommend and not after:
            raise QuestionFileError(
                f"{where}: a `recommend` question needs `after`: the answer it is about"
            )
        questions.append(
            Question(
                id=entry["id"],
                task=entry["task"].strip(),
                question=entry["question"].strip(),
                kind=entry["kind"],
                expect=_expect_list(entry, where),
                cite=cite.strip(),
                must_not=_text_list(entry, "must_not", where),
                after=after.strip(),
                recommend=recommend,
            )
        )
        seen_before.add(entry["id"])
    return questions


def load_questions(path: Path) -> list[Question]:
    import yaml  # imported here so the scoring tests need nothing but the stdlib

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise QuestionFileError(f"{path.name} is not valid YAML: {exc}") from exc
    return parse_questions(data)


def select_questions(
    questions: Sequence[Question], only: str | None = None, kind: str | None = None
) -> list[Question]:
    """Apply `ONLY=<id>` and `KIND=<kind>`; an unknown id or kind is an error."""
    if only is not None and only not in {q.id for q in questions}:
        raise QuestionFileError(
            f"no question with id {only!r}; ids: {', '.join(q.id for q in questions)}"
        )
    if kind is not None and kind not in KINDS:
        raise QuestionFileError(f"kind {kind!r} is not one of {', '.join(KINDS)}")
    chosen = [
        q
        for q in questions
        if (only is None or q.id == only) and (kind is None or q.kind == kind)
    ]
    if not chosen:
        raise QuestionFileError("no questions match those filters")
    # A follow-up needs the answer it follows, so that question comes along (and is
    # asked and scored too).
    by_id = {q.id: q for q in questions}
    needed = {q.id for q in chosen}
    for q in chosen:
        parent = q.after
        while parent and parent not in needed:
            needed.add(parent)
            parent = by_id[parent].after
    return [q for q in questions if q.id in needed]


# --- scoring ---

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def _normalize(text: str) -> str:
    """Lowercase, straighten curly quotes, and collapse any whitespace (including
    non-breaking spaces and line breaks) so wording matches regardless of wrapping."""
    return re.sub(r"\s+", " ", text.translate(_QUOTES)).strip().casefold()


def _contains(haystack: str, needle: str) -> bool:
    return _normalize(needle) in haystack


def _alternatives(item: str | tuple[str, ...]) -> tuple[str, ...]:
    return (item,) if isinstance(item, str) else tuple(item)


def expect_label(item: str | tuple[str, ...]) -> str:
    """How an `expect` item reads in reports: 'a' or 'a / b / c' for alternatives."""
    return " / ".join(_alternatives(item))


@dataclass
class Score:
    score: str  # pass, partial or fail
    missing: list[str] = field(default_factory=list)  # `expect` items not found
    forbidden: list[str] = field(default_factory=list)  # `must_not` items found
    cite_ok: bool | None = None  # None when the question has no `cite`
    reason: str = ""  # one plain sentence


def score_answer(
    question: Question, answer_text: str, source_titles: Sequence[str] = ()
) -> Score:
    """Score one answer. `source_titles` are the titles of the sources the answer links."""
    text = _normalize(answer_text)
    missing = [
        expect_label(item)
        for item in question.expect
        if not any(_contains(text, option) for option in _alternatives(item))
    ]
    forbidden = [item for item in question.must_not if _contains(text, item)]
    cite_ok = None
    if question.cite:
        cite_ok = any(
            _contains(_normalize(title), question.cite) for title in source_titles
        )

    found = len(question.expect) - len(missing)
    all_found = not missing
    half_found = found * 2 >= len(question.expect)

    if all_found and not forbidden and cite_ok is not False:
        return Score(
            PASS, missing, forbidden, cite_ok, "Everything expected was there."
        )

    problems = []
    if missing:
        problems.append(f"missing {', '.join(repr(m) for m in missing)}")
    if forbidden:
        problems.append(f"contains {', '.join(repr(f) for f in forbidden)}")
    if cite_ok is False:
        problems.append(f"no linked source titled like {question.cite!r}")
    reason = "; ".join(problems)
    reason = reason[0].upper() + reason[1:] + "."

    outcome = PARTIAL if half_found and not forbidden else FAIL
    return Score(outcome, missing, forbidden, cite_ok, reason)


# --- one question's result ---


@dataclass
class Outcome:
    """What asking the coach produced, before scoring. `ask` returns one of these."""

    answer: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    seconds: float = 0.0
    model_calls: int = 0
    drive_calls: int | None = 0  # None when the coach failed (the count is lost)
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    cost_reported: bool = True  # False if any model call came back without a cost
    task_source: str = ""  # "database" or "made-up"
    notes: list[str] = field(default_factory=list)  # e.g. "Drive failed"
    error: str | None = None  # plain message if the coach failed
    error_kind: str | None = None  # budget_exceeded, rate_limited, timeout, error
    error_detail: list[str] = field(default_factory=list)  # what the server logged
    # One line per model call: status, finish reason, tokens (reasoning tokens when the
    # model reports them) and whether the live-read tool was called. This is what says
    # WHY an answer was cut off or came back empty.
    call_details: list[str] = field(default_factory=list)
    attempts: int = 1  # 2 if the question was retried after an error


@dataclass
class Result:
    id: str
    kind: str
    task: str
    question: str
    expect: list[str]
    cite: str
    must_not: list[str]
    score: str
    reason: str
    missing: list[str]
    forbidden: list[str]
    cite_ok: bool | None
    outcome: Outcome


def score_outcome(question: Question, outcome: Outcome) -> Result:
    if outcome.error is not None:
        graded = Score(
            FAIL,
            [expect_label(item) for item in question.expect],
            [],
            None,
            f"Error: {outcome.error}",
        )
        if question.cite:
            graded.cite_ok = False
    else:
        titles = [s.get("title", "") for s in outcome.sources]
        graded = score_answer(question, outcome.answer, titles)
    return Result(
        id=question.id,
        kind=question.kind,
        task=question.task,
        question=question.question,
        expect=[expect_label(item) for item in question.expect],
        cite=question.cite,
        must_not=list(question.must_not),
        score=graded.score,
        reason=graded.reason,
        missing=graded.missing,
        forbidden=graded.forbidden,
        cite_ok=graded.cite_ok,
        outcome=outcome,
    )


def not_run(question: Question, reason: str) -> Result:
    return Result(
        id=question.id,
        kind=question.kind,
        task=question.task,
        question=question.question,
        expect=[expect_label(item) for item in question.expect],
        cite=question.cite,
        must_not=list(question.must_not),
        score=NOT_RUN,
        reason=reason,
        missing=[],
        forbidden=[],
        cite_ok=None,
        outcome=Outcome(),
    )


# --- running ---


@dataclass
class Run:
    results: list[Result]
    stopped: str | None  # why the run stopped early, or None


def merge_attempts(first: Outcome, second: Outcome) -> Outcome:
    """Combine a failed first attempt with its retry: the retry's answer or error,
    but the time, calls, tokens and cost of both, so nothing is hidden."""
    return Outcome(
        answer=second.answer,
        sources=second.sources,
        seconds=first.seconds + second.seconds,
        model_calls=first.model_calls + second.model_calls,
        drive_calls=second.drive_calls,
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cost=first.cost + second.cost,
        cost_reported=first.cost_reported and second.cost_reported,
        task_source=second.task_source or first.task_source,
        notes=[*second.notes, f"Retried once after: {first.error}"],
        error=second.error,
        error_kind=second.error_kind,
        error_detail=[*first.error_detail, *second.error_detail],
        call_details=[*first.call_details, *second.call_details],
        attempts=first.attempts + second.attempts,
    )


def run_questions(
    questions: Sequence[Question],
    ask: Callable[[Question, Result | None], Outcome],
    max_cost: float = DEFAULT_MAX_COST,
    on_result: Callable[[Result], None] | None = None,
    pause: float = DEFAULT_PAUSE,
    retry_pause: float = DEFAULT_RETRY_PAUSE,
    sleep: Callable[[float], None] = time.sleep,
    done: Mapping[str, Result] | None = None,
) -> Run:
    """Ask each question in order and score it.

    `ask(question, earlier)` gets the result (question, answer, sources) of the question
    this one follows up (`Question.after`), or None. A follow-up whose earlier question
    failed is not asked: there is no answer to follow up.

    Waits `pause` seconds between questions. A question that fails with a rate limit,
    timeout or model error is retried at most once, after `retry_pause` seconds, and
    the retry's cost counts. Stops before the next question once the total cost
    passes `max_cost` (a retry is skipped if it would start past the limit), or once
    the model's budget is used up.

    `done` holds results from an earlier run (`make eval RESUME=1`): those questions
    are kept as they were, not asked again, and cost nothing here.
    """
    kept = done or {}
    results: list[Result] = []
    scored: dict[str, Result] = {}
    total = 0.0
    stopped: str | None = None
    asked_any = False
    for position, question in enumerate(questions):
        if question.id in kept:
            results.append(kept[question.id])
            scored[question.id] = kept[question.id]
            continue
        if stopped is not None:
            results.append(not_run(question, stopped))
            continue
        earlier = scored.get(question.after) if question.after else None
        if question.after and (earlier is None or earlier.outcome.error is not None):
            results.append(
                not_run(
                    question, f"Skipped: {question.after} has no answer to follow up."
                )
            )
            continue
        if asked_any:
            sleep(pause)
        asked_any = True
        outcome = ask(question, earlier)
        if outcome.error_kind in RETRYABLE and total + outcome.cost <= max_cost:
            sleep(retry_pause)
            outcome = merge_attempts(outcome, ask(question, earlier))
        result = score_outcome(question, outcome)
        results.append(result)
        scored[question.id] = result
        if on_result:
            on_result(result)
        total += outcome.cost
        # Only a stop that leaves questions unasked is worth reporting.
        more = any(later.id not in kept for later in questions[position + 1 :])
        if not more:
            continue
        if outcome.error_kind == "budget_exceeded":
            stopped = "Stopped: the Glassroom budget is used up."
        elif total > max_cost:
            stopped = (
                f"Stopped: total cost ${total:.4f} passed the ${max_cost:.2f} limit."
            )
    return Run(results, stopped)


# --- summary ---


@dataclass
class Summary:
    passed: int
    partial: int
    failed: int
    not_run: int
    errors: int  # questions that ended in an error (counted in `failed` too)
    retried: int  # questions that needed their one retry
    median_seconds: float
    max_seconds: float
    total_cost: float
    cost_complete: bool  # False if some model call had no reported cost
    input_tokens: int = 0  # all questions that ran
    output_tokens: int = 0
    asked: int = 0  # questions that ran (the divisor for the per-question numbers)

    def line(self) -> str:
        failed = f"fail {self.failed}"
        if self.errors:
            failed += f" ({self.errors} error{'' if self.errors == 1 else 's'})"
        parts = [f"pass {self.passed}", f"partial {self.partial}", failed]
        if self.not_run:
            parts.append(f"not run {self.not_run}")
        cost = f"${self.total_cost:.4f}"
        if not self.cost_complete:
            cost += " (some calls reported no cost)"
        line = (
            f"{' / '.join(parts)} | median {self.median_seconds:.1f}s, "
            f"max {self.max_seconds:.1f}s | total cost {cost}"
        )
        if self.asked:
            line += (
                f", ${self.total_cost / self.asked:.4f}/question | tokens per question "
                f"{self.input_tokens // self.asked:,} in / "
                f"{self.output_tokens // self.asked:,} out"
            )
        return line


def summarize(results: Sequence[Result]) -> Summary:
    ran = [r for r in results if r.score != NOT_RUN]
    seconds = [r.outcome.seconds for r in ran]
    return Summary(
        passed=sum(r.score == PASS for r in results),
        partial=sum(r.score == PARTIAL for r in results),
        failed=sum(r.score == FAIL for r in results),
        not_run=sum(r.score == NOT_RUN for r in results),
        errors=sum(r.outcome.error is not None for r in ran),
        retried=sum(r.outcome.attempts > 1 for r in ran),
        median_seconds=statistics.median(seconds) if seconds else 0.0,
        max_seconds=max(seconds, default=0.0),
        total_cost=sum(r.outcome.cost for r in ran),
        cost_complete=all(r.outcome.cost_reported for r in ran),
        input_tokens=sum(r.outcome.input_tokens for r in ran),
        output_tokens=sum(r.outcome.output_tokens for r in ran),
        asked=len(ran),
    )


# --- text output ---


def format_table(results: Sequence[Result]) -> str:
    rows = [("id", "kind", "score", "seconds", "tokens in", "tokens out", "cost")]
    for r in results:
        if r.score == NOT_RUN:
            rows.append((r.id, r.kind, r.score, "-", "-", "-", "-"))
        else:
            rows.append(
                (
                    r.id,
                    r.kind,
                    r.score,
                    f"{r.outcome.seconds:.1f}",
                    f"{r.outcome.input_tokens:,}",
                    f"{r.outcome.output_tokens:,}",
                    f"${r.outcome.cost:.4f}",
                )
            )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for number, row in enumerate(rows):
        cells = [
            cell.ljust(widths[i]) if i < 3 else cell.rjust(widths[i])
            for i, cell in enumerate(row)
        ]
        lines.append("  ".join(cells).rstrip())
        if number == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def render_markdown(meta: dict, run: Run) -> str:
    """The readable report: summary, table, then every question with its full answer."""
    summary = summarize(run.results)
    out = [
        f"# Coach eval, {meta['started_at']}",
        "",
        f"- Model: `{meta['model']}`",
        f"- Code version: `{meta['git']}`",
        f"- Questions: {len(run.results)} ({meta['filters']})",
    ]
    if meta.get("resumed_from"):
        out.append(
            f"- Resumed from `{meta['resumed_from']}`: {meta.get('kept', 0)} answers "
            "kept from that run"
        )
    out += [
        "",
        f"**{summary.line()}**",
    ]
    if run.stopped:
        out += ["", f"**{run.stopped}**"]
    out += ["", "```", format_table(run.results), "```", ""]
    for r in run.results:
        o = r.outcome
        out += [
            f"## {r.id} ({r.kind}): {r.score}",
            "",
            f"Task {r.task}: {r.question}",
            "",
        ]
        if r.score == NOT_RUN:
            out += [r.reason, ""]
            continue
        out.append(f"- Result: {r.reason}")
        if r.expect:
            out.append(f"- Expected: {', '.join(r.expect)}")
        if r.cite:
            out.append(f"- Cite: {r.cite} ({'linked' if r.cite_ok else 'not linked'})")
        if r.must_not:
            out.append(f"- Must not: {', '.join(r.must_not)}")
        drive = "unknown" if o.drive_calls is None else o.drive_calls
        out.append(
            f"- {o.seconds:.1f}s, {o.model_calls} model calls, {drive} live Drive "
            f"reads, {o.input_tokens} in / {o.output_tokens} out tokens, ${o.cost:.4f}"
        )
        if o.task_source:
            out.append(f"- Task context from: {o.task_source}")
        out += [f"- Model call: {line}" for line in o.call_details]
        out += [f"- Note: {note}" for note in o.notes]
        out.append("")
        if o.error:
            out += [f"**Error ({o.error_kind}):** {o.error}"]
            out += [f"- Server log: {line}" for line in o.error_detail]
            out.append("")
        else:
            out += [_quote(o.answer), ""]
        if o.sources:
            out.append("Sources linked:")
            out += [f"- {s.get('title', '')}: {s.get('link', '')}" for s in o.sources]
            out.append("")
    return "\n".join(out)


def write_reports(
    directory: Path, stamp: str, meta: dict, run: Run
) -> tuple[Path, Path]:
    """Write <stamp>.json (everything, for diffing runs) and <stamp>.md (for reading)."""
    directory.mkdir(parents=True, exist_ok=True)
    summary = summarize(run.results)
    data = {
        **meta,
        "stopped": run.stopped,
        "summary": {**asdict(summary), "line": summary.line()},
        "results": [asdict(r) for r in run.results],
    }
    json_path = directory / f"{stamp}.json"
    md_path = directory / f"{stamp}.md"
    json_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(meta, run) + "\n", encoding="utf-8")
    return json_path, md_path


# --- resuming a stopped run (make eval RESUME=1) ---


def latest_report(directory: Path) -> Path | None:
    """The newest <timestamp>.json report in `directory`, or None."""
    found = sorted(directory.glob("*.json")) if directory.is_dir() else []
    return found[-1] if found else None


def _known(cls: type, data: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def result_from_dict(data: dict) -> Result:
    """Rebuild a `Result` from a report's JSON (older reports lack newer fields)."""
    outcome = Outcome(**_known(Outcome, data.get("outcome") or {}))
    return Result(**{**_known(Result, data), "outcome": outcome})


def results_to_keep(report: dict, questions: Sequence[Question]) -> dict[str, Result]:
    """The results of an earlier run that RESUME keeps: every question that was asked
    (not "not run") and has not been edited since. The rest are asked again."""
    current = {q.id: q for q in questions}
    kept: dict[str, Result] = {}
    for raw in report.get("results", []):
        result = result_from_dict(raw)
        question = current.get(result.id)
        if (
            question is not None
            and result.score != NOT_RUN
            and result.question == question.question
            and result.task == question.task
        ):
            kept[result.id] = result
    return kept


# --- retrieval only (make eval RETRIEVAL=1): what the coach would be shown ---

CHARS_PER_TOKEN = 4  # a rough rule for English; the real count comes from the model


@dataclass
class ContextCheck:
    """Whether the material given to the model could answer a question. No model."""

    id: str
    tokens: int  # estimated size of the whole prompt
    expect_found: int
    expect_total: int
    missing: list[str]
    cite_ok: bool | None


def check_context(
    question: Question, prompt_text: str, source_titles: Sequence[str]
) -> ContextCheck:
    """Which `expect` phrases and which `cite` title are in the prompt the model gets.

    A proxy for retrieval: an answer can't contain a fact that was never shown, so a
    phrase missing here is a retrieval miss, whatever the model then does.
    """
    text = _normalize(prompt_text)
    missing = [
        expect_label(item)
        for item in question.expect
        if not any(_contains(text, option) for option in _alternatives(item))
    ]
    cite_ok = None
    if question.cite:
        cite_ok = any(
            _contains(_normalize(title), question.cite) for title in source_titles
        )
    return ContextCheck(
        id=question.id,
        tokens=len(prompt_text) // CHARS_PER_TOKEN,
        expect_found=len(question.expect) - len(missing),
        expect_total=len(question.expect),
        missing=missing,
        cite_ok=cite_ok,
    )


def format_context_table(checks: Sequence[ContextCheck]) -> str:
    rows = [("id", "est. tokens in", "expect shown", "cite shown", "not shown")]
    for c in checks:
        cite = "-" if c.cite_ok is None else ("yes" if c.cite_ok else "NO")
        rows.append(
            (
                c.id,
                f"{c.tokens:,}",
                f"{c.expect_found}/{c.expect_total}",
                cite,
                ", ".join(c.missing),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    lines = []
    for number, row in enumerate(rows):
        cells = [
            cell.ljust(widths[0]) if i == 0 else cell.rjust(widths[i])
            for i, cell in enumerate(row[:4])
        ]
        lines.append(("  ".join(cells) + "  " + row[4]).rstrip())
        if number == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def context_summary(checks: Sequence[ContextCheck]) -> str:
    """One line for a PR: how many questions had everything they need in the prompt."""
    full = sum(
        c.expect_found == c.expect_total and c.cite_ok is not False for c in checks
    )
    tokens = sorted(c.tokens for c in checks)
    median = tokens[len(tokens) // 2] if tokens else 0
    return (
        f"context complete {full} / {len(checks)} | "
        f"estimated tokens in: median {median:,}, max {max(tokens, default=0):,}"
    )
