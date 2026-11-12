"""Run the coach test questions and score the answers: `make eval`.

    make eval                    # every question in evals/coach_questions.yaml
    make eval ONLY=lid-latch     # one question
    make eval KIND=conflict      # one kind

Uses the REAL model and the REAL Drive, so it costs money. It stops once the total
cost passes $0.40 (override with --max-cost). The Glassroom cap is $1 per 5 hours
across everyone, so check before running twice in a row.

It waits 3 seconds between questions (--pause). A question that fails with a rate
limit, timeout or model error is retried once after 20 seconds (--retry-pause); the
retry's time, tokens and cost are added to that question's. The summary counts such
questions as fails and also says how many were errors.

Each question is asked the way the web page asks it: the task's context comes from
`coach.task_context` (from the local database when the task is there, otherwise a
made-up task with that code), the passages from `retrieval.retrieve` on the local Drive
index (so DATABASE_URL must point at a synced one), and the answer from
`answering.AnswerRun`, with an empty chat history. Full answers go to
.context/evals/<timestamp>.json and .md (gitignored).
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
QUESTIONS = ROOT / "evals" / "coach_questions.yaml"
OUT = ROOT / ".context" / "evals"

# A script is imported relative to src/scripts, so add src for `import llm`.
sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
# `import coach` loads the database module, which insists on a URL. A missing one
# just means every task is made up, so point it at an empty in-memory database.
os.environ.setdefault("DATABASE_URL", "sqlite://")

import answering
import coach
import coach_eval
import db
import llm
import retrieval
from models import Task

log = logging.getLogger("crush_coach")


def missing_setup() -> str | None:
    """A plain message if the real model or the Drive index can't be reached, else None.

    Without these every question would "fail" for the same reason, which says
    nothing about the coach, so the run refuses to start.
    """
    if not os.environ.get("GLASSROOM_API_KEY"):
        return "GLASSROOM_API_KEY is not set (see .env.example)."
    try:
        from sqlmodel import Session

        with Session(db.engine) as session:
            if retrieval.index_is_empty(session):
                return (
                    "The Drive index in DATABASE_URL is empty. Run "
                    "scripts/sync_drive.py --full first."
                )
    except Exception as exc:  # noqa: BLE001  (no database, or not migrated)
        return f"Can't read the Drive index in DATABASE_URL ({type(exc).__name__})."
    return None


def find_task(code: str) -> tuple[Task, str]:
    """The task from the local database, or a made-up one with that code."""
    try:
        from sqlmodel import Session, select

        with Session(db.engine) as session:
            task = session.exec(select(Task).where(Task.code == code)).first()
            if task is not None:
                return task, "database"
    except Exception as exc:  # noqa: BLE001  (no database running: use a made-up task)
        log.debug("No task %s in the local database (%s)", code, type(exc).__name__)
    made_up = Task(
        code=code,
        title=f"Eval task {code}",
        subsystem="Enclosure",
        task_type="Fabricate",
        stage="drawing",
    )
    return made_up, "made-up"


class _LogCapture(logging.Handler):
    """Collects what the server would log while one question runs. A member sees
    "The AI model failed to answer"; the log says which kind of failure it was."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def answer_question(
    task: Task, question: str, calls: list[llm.CallRecord]
) -> llm.CoachAnswer:
    """Retrieval, then one streamed answer: the same two steps the web page runs."""
    from sqlmodel import Session

    with Session(db.engine) as session:
        found = retrieval.retrieve(session, task, question)
    run = answering.AnswerRun(
        coach.task_context(task),
        found,
        question,
        [],
        today=coach.today(),
        record=calls.append,
    )
    return run.run()


def ask(question: coach_eval.Question) -> coach_eval.Outcome:
    """Ask the coach one question exactly as the web page would, and time it."""
    task, task_source = find_task(question.task)
    calls: list[llm.CallRecord] = []
    outcome = coach_eval.Outcome(task_source=task_source)
    capture = _LogCapture()
    log.addHandler(capture)
    started = time.monotonic()
    try:
        answer = answer_question(task, question.question, calls)
    except llm.LLMError as exc:
        outcome.error, outcome.error_kind = exc.message, exc.kind
        outcome.drive_calls = None  # the count is lost when the coach raises
    except retrieval.IndexEmpty:
        outcome.error, outcome.error_kind = "The Drive index is empty.", "error"
        outcome.drive_calls = None
    except Exception as exc:  # noqa: BLE001  (one bad question must not end the run)
        outcome.error = f"Unexpected failure ({type(exc).__name__})."
        outcome.error_kind = "error"
        outcome.drive_calls = None
    else:
        outcome.answer = answer.text
        outcome.sources = answer.sources
        outcome.drive_calls = answer.tool_calls
        outcome.notes.extend(answering.notes_for(answer))
    finally:
        log.removeHandler(capture)
    outcome.error_detail = capture.lines
    outcome.seconds = time.monotonic() - started
    outcome.model_calls = len(calls)
    outcome.input_tokens = sum(c.input_tokens or 0 for c in calls)
    outcome.output_tokens = sum(c.output_tokens or 0 for c in calls)
    outcome.cost = sum(c.cost or 0.0 for c in calls)
    # A failed call has no cost to report; only a successful one should.
    outcome.cost_reported = all(c.cost is not None for c in calls if c.status == "ok")
    return outcome


def git_version() -> str:
    """Short commit, plus '+uncommitted changes' - so two runs can be compared fairly."""
    try:

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
            ).stdout.strip()

        dirty = git("status", "--porcelain", "--", "src", "evals")
        return git("rev-parse", "--short", "HEAD") + (
            " +uncommitted changes" if dirty else ""
        )
    except Exception:  # noqa: BLE001  (no git: the report just says unknown)
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score the Crush coach on test questions"
    )
    parser.add_argument("--only", help="run just the question with this id")
    parser.add_argument("--kind", help=f"run one kind: {', '.join(coach_eval.KINDS)}")
    parser.add_argument(
        "--max-cost",
        type=float,
        default=coach_eval.DEFAULT_MAX_COST,
        help="stop once the total cost passes this many dollars (default: %(default)s)",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=coach_eval.DEFAULT_PAUSE,
        help="seconds to wait between questions (default: %(default)s)",
    )
    parser.add_argument(
        "--retry-pause",
        type=float,
        default=coach_eval.DEFAULT_RETRY_PAUSE,
        help="seconds to wait before the one retry of a failed question "
        "(default: %(default)s)",
    )
    args = parser.parse_args()

    try:
        questions = coach_eval.select_questions(
            coach_eval.load_questions(QUESTIONS), args.only or None, args.kind or None
        )
    except (coach_eval.QuestionFileError, OSError) as exc:
        print(f"Cannot run: {exc}", file=sys.stderr)
        return 2

    problem = missing_setup()
    if problem:
        print(f"Cannot run: {problem}", file=sys.stderr)
        return 2

    print(f"Asking the coach {len(questions)} question(s) with {llm.model_name()}...\n")

    def show(result: coach_eval.Result) -> None:
        o = result.outcome
        print(
            f"  {result.id}: {result.score}  {o.seconds:.1f}s  ${o.cost:.4f}"
            + ("  (retried)" if o.attempts > 1 else "")
            + (f"  [{result.reason}]" if result.score != coach_eval.PASS else ""),
            flush=True,
        )
        for line in o.error_detail:
            print(f"      server log: {line}", flush=True)

    started = datetime.now().astimezone()
    run = coach_eval.run_questions(
        questions,
        ask,
        args.max_cost,
        on_result=show,
        pause=args.pause,
        retry_pause=args.retry_pause,
    )

    print("\n" + coach_eval.format_table(run.results))
    if run.stopped:
        print(f"\n{run.stopped} The questions after that were not run.")
    summary = coach_eval.summarize(run.results)
    print(f"\n{summary.line()}")

    filters = ", ".join(
        f"{name}={value}"
        for name, value in (("only", args.only), ("kind", args.kind))
        if value
    )
    meta = {
        "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "model": llm.model_name(),
        "git": git_version(),
        "filters": filters or "all",
        "max_cost": args.max_cost,
    }
    json_path, md_path = coach_eval.write_reports(
        OUT, started.strftime("%Y%m%d-%H%M%S"), meta, run
    )
    print(
        f"\nFull answers: {md_path.relative_to(ROOT)}\n              {json_path.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
