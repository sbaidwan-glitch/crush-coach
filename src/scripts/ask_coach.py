"""Ask the coach one real question: real model, real Drive, no database or server.

    cd src && ../.venv/bin/python scripts/ask_coach.py "How do I mount the latch?"

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
    parser.add_argument("--code", default="M04", help="task code (default: M04)")
    parser.add_argument(
        "--title", default="Finish enclosure wall panels", help="task title"
    )
    parser.add_argument("--subsystem", default="Enclosure")
    parser.add_argument("--type", dest="task_type", default="Fabricate")
    parser.add_argument("--stage", default="drawing")
    args = parser.parse_args()

    calls: list[llm.CallRecord] = []
    try:
        answer = llm.run_coach(
            {
                "Code": args.code,
                "Title": args.title,
                "Subsystem": args.subsystem,
                "Type": args.task_type,
                "Stage": args.stage,
            },
            [],
            args.question,
            record=calls.append,
        )
    except llm.LLMError as exc:
        print(f"Coach failed ({exc.kind}): {exc.message}", file=sys.stderr)
        return 1

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
    print(f"\nModel: {llm.model_name()}; tool calls: {answer.tool_calls}")
    for number, call in enumerate(calls, start=1):
        print(
            f"  call {number}: {call.status}, {call.input_tokens} in / "
            f"{call.output_tokens} out tokens, {call.latency_ms} ms, "
            f"cost {call.cost if call.cost is not None else 'not reported'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
