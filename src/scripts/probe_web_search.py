"""Does Glassroom let each model search the web? One tiny request per model.

    cd src && ../.venv/bin/python scripts/probe_web_search.py
    cd src && ../.venv/bin/python scripts/probe_web_search.py claude-haiku-4-5

A model being ABLE to search does not mean the proxy turns it on: search is a tool
the request has to ask for (`web_search_options`), and the proxy has to pass it to the
provider. So this asks each model something only a live search can answer and prints
what came back: an error, an answer with no citations (the model answered from memory),
or an answer with web citations (search works). Costs a cent or two per model, and
search is billed per search on top of tokens. Prints no secrets and no prompt text
beyond the question below.
"""

import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import llm

MODELS = (
    "qwen3.6-35b-instruct",
    "gemini-3.5-flash-lite",
    "claude-sonnet-5",
    "claude-haiku-4-5",
)
QUESTION = (
    "Search the web and tell me the title of the number one story on Hacker News "
    "right now (news.ycombinator.com), and give the URL you found it at."
)


def citations(message: Any) -> list[str]:
    """The web URLs the reply cites (OpenAI-style `url_citation` annotations)."""
    found = []
    for note in getattr(message, "annotations", None) or []:
        detail = getattr(note, "url_citation", None)
        url = getattr(detail, "url", None) if detail else getattr(note, "url", None)
        if url:
            found.append(str(url))
    return found


def probe(model: str) -> str:
    started = time.monotonic()
    try:
        raw = llm.get_client().chat.completions.with_raw_response.create(
            model=model,
            messages=[{"role": "user", "content": QUESTION}],
            max_tokens=600,
            extra_body={"web_search_options": {"search_context_size": "low"}},
        )
        reply = raw.parse()
    except llm.LLMError as exc:
        return f"{model}: not run ({exc.message})"
    except Exception as exc:  # noqa: BLE001  (a probe reports every failure plainly)
        status = getattr(exc, "status_code", "")
        text = str(getattr(exc, "message", ""))[:160].replace("\n", " ")
        return f"{model}: ERROR {type(exc).__name__} {status} {text}"
    message = reply.choices[0].message
    urls = citations(message)
    cost = raw.headers.get(llm.COST_HEADER, "not reported")
    verdict = (
        "SEARCH WORKS (web citations)"
        if urls
        else "no citations (answered from memory?)"
    )
    answer = " ".join((message.content or "").split())[:200]
    return (
        f"{model}: {verdict}; {len(urls)} citation(s), cost {cost}, "
        f"{time.monotonic() - started:.1f}s\n    {answer}\n    "
        + "\n    ".join(urls[:3])
    )


def main() -> int:
    if not os.environ.get("GLASSROOM_API_KEY"):
        print("GLASSROOM_API_KEY is not set (see .env.example).", file=sys.stderr)
        return 2
    for model in sys.argv[1:] or MODELS:
        print(probe(model), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
