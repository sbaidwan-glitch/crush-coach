"""Ask the coach one real question: real model, the local Drive index, no server.

    cd src && ../.venv/bin/python scripts/ask_coach.py "How do I mount the latch?"

    # A two-turn chat on one task (the second question sees the first answer):
    cd src && ../.venv/bin/python scripts/ask_coach.py --code E01 \\
        --title "Draft tray layout" --subsystem "Electronics Mounts" --type Design \\
        --stage CAD "How do I start?" --follow-up "Where is it?"

Needs GLASSROOM_API_KEY and DATABASE_URL pointing at a database with a synced Drive
index (`scripts/sync_drive.py`). The Google credentials are only needed for the rare
live read of a Doc. Prints the steps, the answer as it is written, the sources it
cited, and the timing, tokens and cost of the model call.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlmodel import Session

# A script is imported relative to src/scripts, so add src for `import llm`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import answering
import coach
import llm
import retrieval
from db import engine
from models import Task


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    parser = argparse.ArgumentParser(description="Ask the Crush coach one question")
    parser.add_argument("question")
    parser.add_argument(
        "--follow-up",
        help="a second question, asked after the first answer (it sees that chat)",
    )
    parser.add_argument("--code", default="M04", help="task code (default: M04)")
    parser.add_argument(
        "--title", default="Finish enclosure wall panels", help="task title"
    )
    parser.add_argument("--subsystem", default="Enclosure")
    parser.add_argument("--type", dest="task_type", default="Fabricate")
    parser.add_argument("--stage", default="drawing")
    args = parser.parse_args()

    task = Task(
        code=args.code,
        title=args.title,
        subsystem=args.subsystem,
        task_type=args.task_type,
        stage=args.stage,
    )
    history: list[dict[str, str]] = []
    questions = [args.question] + ([args.follow_up] if args.follow_up else [])
    earlier: list[dict] = []
    previous = ""
    for number, question in enumerate(questions, start=1):
        if len(questions) > 1:
            print(f"\n=== Question {number}: {question}")
        answer = ask(task, history, question, previous, earlier)
        if answer is None:
            return 1
        history += [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer.text},
        ]
        earlier = answer.sources + [e for e in earlier if e not in answer.sources]
        previous = question
    return 0


def ask(
    task: Task,
    history: list[dict[str, str]],
    question: str,
    previous: str = "",
    earlier: list[dict] | None = None,
) -> llm.CoachAnswer | None:
    calls: list[llm.CallRecord] = []
    try:
        with Session(engine) as session:
            print("  [Searching the team Drive index…]", flush=True)
            found = retrieval.retrieve(
                session,
                task,
                question,
                previous_question=previous,
                earlier_sources=earlier,
            )
        print(
            f"  [{len(found.chunks)} passages, {len(found.cad)} CAD files, "
            f"{len(found.decisions)} decisions, {len(found.facts)} facts "
            f"in {found.seconds:.2f}s]",
            flush=True,
        )
        run = answering.AnswerRun(
            coach.task_context(task),
            found,
            question,
            history,
            today=coach.today(),
            record=calls.append,
        )
        print()
        shown = ""
        for event in run:
            if event["type"] == "step":
                print(f"  [{event['text']}]", flush=True)
            elif event["type"] == "text":
                print(event["text"][len(shown) :], end="", flush=True)
                shown = event["text"]
        answer = run.answer
    except retrieval.IndexEmpty:
        print("Drive index is empty: run scripts/sync_drive.py first.", file=sys.stderr)
        return None
    except llm.LLMError as exc:
        print(f"Coach failed ({exc.kind}): {exc.message}", file=sys.stderr)
        return None

    assert answer is not None
    print("\n")
    if answer.text != shown:  # links the model invented were taken out at the end
        print(answer.text)
    if answer.sources:
        print("Sources:")
        for source in answer.sources:
            print(f"  {source['title']} (modified {source['modified']})")
            print(f"    {source['link']}")
    for note in answering.notes_for(answer):
        print(f"\nNote: {note}")
    print(f"\nModel: {llm.model_name()}; live reads: {answer.tool_calls}")
    first = answer.first_text_seconds
    print(
        f"  total {answer.seconds:.1f}s, first text "
        f"{'n/a' if first is None else f'{first:.1f}s'}"
    )
    for number, call in enumerate(calls, start=1):
        cost = "not reported"
        if call.cost is not None:
            cost = f"{call.cost:.4f}" + (" (estimated)" if call.cost_estimated else "")
        print(
            f"  call {number}: {call.status}, {call.input_tokens} in / "
            f"{call.output_tokens} out tokens, {call.latency_ms} ms, cost {cost}"
        )
    return answer


if __name__ == "__main__":
    raise SystemExit(main())
