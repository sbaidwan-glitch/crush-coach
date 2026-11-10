"""Ask the coach one real question: real model, real Drive, no database or server.

    cd src && ../.venv/bin/python scripts/ask_coach.py "How do I mount the latch?"

    # A two-turn chat on one task (the second question sees the first answer):
    cd src && ../.venv/bin/python scripts/ask_coach.py --code E01 \\
        --title "Draft tray layout" --subsystem "Electronics Mounts" --type Design \\
        --stage CAD "How do I start?" --follow-up "Where is it?"

Needs GLASSROOM_API_KEY plus the Google credentials (see .env.example). Prints the
answer, the sources it cited, and the token counts and cost of each model call.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# A script is imported relative to src/scripts, so add src for `import llm`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm


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

    task = {
        "Code": args.code,
        "Title": args.title,
        "Subsystem": args.subsystem,
        "Type": args.task_type,
        "Stage": args.stage,
    }
    history: list[dict[str, str]] = []
    questions = [args.question] + ([args.follow_up] if args.follow_up else [])
    for number, question in enumerate(questions, start=1):
        if len(questions) > 1:
            print(f"\n=== Question {number}: {question}")
        answer = ask(task, history, question)
        if answer is None:
            return 1
        history += [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer.text},
        ]
    return 0


def ask(
    task: dict[str, str], history: list[dict[str, str]], question: str
) -> llm.CoachAnswer | None:
    calls: list[llm.CallRecord] = []
    try:
        answer = llm.run_coach(
            task,
            history,
            question,
            record=calls.append,
            on_step=lambda step: print(f"  [{step}]", flush=True),
        )
    except llm.LLMError as exc:
        print(f"Coach failed ({exc.kind}): {exc.message}", file=sys.stderr)
        return None

    print()
    print(answer.text)
    if answer.sources:
        print("\nSources:")
        for source in answer.sources:
            print(f"  {source['title']} (modified {source['modified']})")
            print(f"    {source['link']}")
    if answer.drive_failed:
        print("\nNote: Google Drive failed during this answer.")
    if answer.unsourced_numbers:
        print("\nNote: the answer has numbers without a source.")
    if answer.ungrounded_links:
        print("\nNote: a link in the model's draft did not come from a search.")
    print(f"\nModel: {llm.model_name()}; tool calls: {answer.tool_calls}")
    for number, call in enumerate(calls, start=1):
        print(
            f"  call {number}: {call.status}, {call.input_tokens} in / "
            f"{call.output_tokens} out tokens, {call.latency_ms} ms, "
            f"cost {call.cost if call.cost is not None else 'not reported'}"
        )
    return answer


if __name__ == "__main__":
    raise SystemExit(main())
