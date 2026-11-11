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
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

KINDS = ("lookup", "how-to", "why", "conflict", "unknown")
DEFAULT_MAX_COST = 0.40  # dollars; the Glassroom cap is $1 per 5 hours
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
        questions.append(
            Question(
                id=entry["id"],
                task=entry["task"].strip(),
                question=entry["question"].strip(),
                kind=entry["kind"],
                expect=_expect_list(entry, where),
                cite=cite.strip(),
                must_not=_text_list(entry, "must_not", where),
            )
        )
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
    return chosen


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
        attempts=first.attempts + second.attempts,
    )


def run_questions(
    questions: Sequence[Question],
    ask: Callable[[Question], Outcome],
    max_cost: float = DEFAULT_MAX_COST,
    on_result: Callable[[Result], None] | None = None,
    pause: float = DEFAULT_PAUSE,
    retry_pause: float = DEFAULT_RETRY_PAUSE,
    sleep: Callable[[float], None] = time.sleep,
) -> Run:
    """Ask each question in order and score it.

    Waits `pause` seconds between questions. A question that fails with a rate limit,
    timeout or model error is retried at most once, after `retry_pause` seconds, and
    the retry's cost counts. Stops before the next question once the total cost
    passes `max_cost` (a retry is skipped if it would start past the limit), or once
    the model's budget is used up.
    """
    results: list[Result] = []
    total = 0.0
    stopped: str | None = None
    asked_any = False
    for question in questions:
        if stopped is not None:
            results.append(not_run(question, stopped))
            continue
        if asked_any:
            sleep(pause)
        asked_any = True
        outcome = ask(question)
        if outcome.error_kind in RETRYABLE and total + outcome.cost <= max_cost:
            sleep(retry_pause)
            outcome = merge_attempts(outcome, ask(question))
        result = score_outcome(question, outcome)
        results.append(result)
        if on_result:
            on_result(result)
        total += outcome.cost
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
        return (
            f"{' / '.join(parts)} | median {self.median_seconds:.1f}s, "
            f"max {self.max_seconds:.1f}s | total cost {cost}"
        )


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
    )


# --- text output ---


def format_table(results: Sequence[Result]) -> str:
    rows = [("id", "kind", "score", "seconds", "cost")]
    for r in results:
        if r.score == NOT_RUN:
            rows.append((r.id, r.kind, r.score, "-", "-"))
        else:
            rows.append(
                (
                    r.id,
                    r.kind,
                    r.score,
                    f"{r.outcome.seconds:.1f}",
                    f"${r.outcome.cost:.4f}",
                )
            )
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
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
            f"- {o.seconds:.1f}s, {o.model_calls} model calls, {drive} Drive "
            f"calls, {o.input_tokens} in / {o.output_tokens} out tokens, ${o.cost:.4f}"
        )
        if o.task_source:
            out.append(f"- Task context from: {o.task_source}")
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
