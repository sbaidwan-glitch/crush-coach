"""All model access for the coach lives here (and only here).

Glassroom is an OpenAI-compatible LiteLLM proxy, so the `openai` library is the
client. The model is called only from `run_coach`, which `coach.py` calls only when
a member sends a message. Nothing in this module logs the API key or prompt text.
"""

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import openai
from openai import OpenAI

import drive

log = logging.getLogger("crush_coach")

BASE_URL = "https://litellm.glassroomai.com/v1"
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TIMEOUT_SECONDS = 30.0
SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "coach_system.md"

MAX_TOOL_CALLS = 4  # per answer
MAX_SEARCH_RESULTS = 8
MAX_DOC_CHARS = 12_000
OLD_AFTER_YEARS = 2  # a source older than this is flagged possibly_old
COST_HEADER = "x-litellm-response-cost"

# OpenAI tool names can't contain dots, so drive.search is `drive_search`.
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "drive_search",
            "description": (
                "Search the team's shared Google Drive by file name and document "
                "text. Use one or two short keywords (e.g. 'latch', 'O-ring "
                "groove'). Returns titles, links, folder paths and modified dates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Short keywords."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drive_read_text",
            "description": (
                "Read the plain text of one Google Doc found by drive_search. "
                "Pass the link exactly as drive_search returned it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "link": {
                        "type": "string",
                        "description": "Drive link (or file id) from drive_search.",
                    }
                },
                "required": ["link"],
            },
        },
    },
]


class LLMError(RuntimeError):
    """A model failure with a message that is safe to show to a member.

    `kind` is one of budget_exceeded, rate_limited, timeout, error.
    """

    def __init__(self, kind: str, message: str, latency_ms: int = 0):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.latency_ms = latency_ms


@dataclass
class CallRecord:
    """What `llm_call` stores for one model call. Never includes prompt text."""

    model: str
    latency_ms: int
    status: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None


@dataclass
class CoachAnswer:
    text: str
    sources: list[dict[str, str]] = field(default_factory=list)
    drive_failed: bool = False
    unsourced_numbers: bool = False
    tool_calls: int = 0


def model_name() -> str:
    return os.environ.get("COACH_MODEL") or DEFAULT_MODEL


@lru_cache(maxsize=1)
def system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def get_client() -> OpenAI:
    api_key = os.environ.get("GLASSROOM_API_KEY")
    if not api_key:
        raise LLMError(
            "error", "The coach isn't set up yet: no model key is configured."
        )
    timeout = float(os.environ.get("COACH_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
    return OpenAI(base_url=BASE_URL, api_key=api_key, timeout=timeout, max_retries=0)


def _classify(exc: Exception, latency_ms: int) -> LLMError:
    """Turn an openai exception into a safe message. Never echoes the request."""
    if isinstance(exc, openai.APITimeoutError):
        return LLMError(
            "timeout", "The AI model took too long to answer. Try again.", latency_ms
        )
    if isinstance(exc, openai.APIConnectionError):
        return LLMError(
            "timeout",
            "The AI model couldn't be reached. Try again in a moment.",
            latency_ms,
        )
    if isinstance(exc, openai.APIStatusError):
        detail = f"{getattr(exc, 'message', '')} {getattr(exc, 'body', '')}".lower()
        if "budget" in detail:
            return LLMError(
                "budget_exceeded",
                "The coach's AI budget is used up for now. Ask a lead to raise it.",
                latency_ms,
            )
        if isinstance(exc, openai.RateLimitError):
            return LLMError(
                "rate_limited",
                "The AI model is busy (rate limit). Wait a minute and try again.",
                latency_ms,
            )
        if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
            return LLMError(
                "error",
                "The coach's model key was rejected. Ask a lead to check the setup.",
                latency_ms,
            )
    return LLMError(
        "error", "The AI model failed to answer. Try again in a moment.", latency_ms
    )


@dataclass
class _Completion:
    message: Any
    record: CallRecord


def _complete(messages: list[dict[str, Any]], allow_tools: bool) -> _Completion:
    model = model_name()
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "tools": TOOLS}
    if not allow_tools:
        # Keep `tools` (Anthropic requires it once tool results are in the
        # conversation) but forbid more calls.
        kwargs["tool_choice"] = "none"

    started = time.monotonic()
    try:
        raw = get_client().chat.completions.with_raw_response.create(**kwargs)
        response = raw.parse()
    except LLMError:
        raise
    except openai.OpenAIError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        # Type only: the message can echo request details.
        log.error("Coach model call failed (%s)", type(exc).__name__)
        raise _classify(exc, latency_ms) from None
    except Exception as exc:  # noqa: BLE001  (never let a model failure break the page)
        latency_ms = int((time.monotonic() - started) * 1000)
        log.error("Coach model call failed (%s)", type(exc).__name__)
        raise _classify(exc, latency_ms) from None

    latency_ms = int((time.monotonic() - started) * 1000)
    usage = getattr(response, "usage", None)
    cost: float | None = None
    try:
        header = raw.headers.get(COST_HEADER)
        cost = float(header) if header is not None else None
    except (AttributeError, TypeError, ValueError):
        cost = None
    record = CallRecord(
        model=model,
        latency_ms=latency_ms,
        status="ok",
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        cost=cost,
    )
    return _Completion(message=response.choices[0].message, record=record)


# --- Drive tools -----------------------------------------------------------

_FILE_ID_PATTERNS = (
    re.compile(r"/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
)


def drive_file_id(link_or_id: str) -> str | None:
    """Pull the file id out of a Drive link; accept a bare id too."""
    value = link_or_id.strip()
    for pattern in _FILE_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    return None


def _is_possibly_old(modified: str, folder_path: str, title: str) -> bool:
    if "alfie" in f"{folder_path} {title}".casefold():
        return True
    try:
        year = int(modified[:4])
    except ValueError:
        return False
    return year <= datetime.now(UTC).year - OLD_AFTER_YEARS


class _DriveTools:
    """Runs the model's tool calls and remembers what Drive returned."""

    def __init__(self) -> None:
        self.calls = 0
        self.drive_failed = False
        self.seen: dict[str, dict[str, str]] = {}  # link -> title, modified

    def _remember(self, link: str, title: str, modified: str) -> None:
        self.seen[link] = {"title": title, "link": link, "modified": modified[:10]}

    def run(self, name: str, arguments: str) -> str:
        self.calls += 1
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise TypeError
        except (ValueError, TypeError):
            return json.dumps({"error": "Tool arguments were not valid JSON."})
        try:
            if name == "drive_search":
                return self._search(str(args.get("query", "")).strip())
            if name == "drive_read_text":
                return self._read(str(args.get("link", "")))
            return json.dumps({"error": f"Unknown tool {name!r}."})
        except Exception as exc:  # noqa: BLE001  (Drive must never break the chat)
            self.drive_failed = True
            log.error("Coach Drive tool failed (%s)", type(exc).__name__)
            message = (
                str(exc)
                if isinstance(exc, drive.DriveError)
                else "Google Drive failed."
            )
            return json.dumps(
                {
                    "error": f"Google Drive failed: {message}",
                    "instruction": "Tell the member Drive could not be reached.",
                }
            )

    def _search(self, query: str) -> str:
        if not query:
            return json.dumps({"error": "drive_search needs a query."})
        results = drive.search(query, MAX_SEARCH_RESULTS)
        if not results:
            return json.dumps({"results": [], "note": "No matching documents."})
        shaped = []
        for result in results:
            modified = result["modified_time"]
            self._remember(result["link"], result["title"], modified)
            others = []
            for location in result["locations"][1:3]:
                self._remember(
                    location["link"], result["title"], location["modified_time"]
                )
                others.append(
                    {
                        "link": location["link"],
                        "folder_path": location["folder_path"],
                        "modified": location["modified_time"][:10],
                    }
                )
            shaped.append(
                {
                    "title": result["title"],
                    "link": result["link"],
                    "folder_path": result["folder_path"],
                    "modified": modified[:10],
                    "possibly_old": _is_possibly_old(
                        modified, result["folder_path"], result["title"]
                    ),
                    "other_copies": others,
                }
            )
        return json.dumps({"results": shaped})

    def _read(self, link: str) -> str:
        file_id = drive_file_id(link)
        if not file_id:
            return json.dumps({"error": "That is not a Drive link or file id."})
        text = drive.read_text(file_id)
        truncated = len(text) > MAX_DOC_CHARS
        return json.dumps(
            {
                "document_text_untrusted": text[:MAX_DOC_CHARS],
                "truncated": truncated,
            }
        )


# --- Answer checks ---------------------------------------------------------

_MACHINING_NUMBER = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:rpm|ipm|sfm|sfpm|ipt|mm|thou|mils?|in-lb|ft-lb|n·m|inch(?:es)?)\b"
    r"|\d\s*\"(?=\W|$)"
    r"|#\d+-\d+"
    r"|±\s*\d",
    re.IGNORECASE,
)


def has_unsourced_numbers(text: str, sources: list[dict[str, str]]) -> bool:
    """Heuristic backstop: machining-looking numbers with no Drive source cited."""
    return not sources and bool(_MACHINING_NUMBER.search(text))


# --- The coach -------------------------------------------------------------


def _assistant_message(message: Any) -> dict[str, Any]:
    """Re-send the model's own tool request, built by hand (SDK-type independent)."""
    entry: dict[str, Any] = {"role": "assistant", "content": message.content}
    entry["tool_calls"] = [
        {
            "id": call.id,
            "type": "function",
            "function": {
                "name": call.function.name,
                "arguments": call.function.arguments,
            },
        }
        for call in message.tool_calls
    ]
    return entry


def run_coach(
    task: dict[str, str],
    history: list[dict[str, str]],
    user_message: str,
    record: Callable[[CallRecord], None] | None = None,
) -> CoachAnswer:
    """Answer one member message. Raises LLMError if the model fails.

    `history` is earlier {role, content} turns. `record` is called once for every
    model call, successful or not, so the caller can log it.
    """
    context = "\n".join(f"- {label}: {value}" for label, value in task.items())
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": f"{system_prompt()}\n\n## This task\n{context}",
        },
        *history,
        {"role": "user", "content": user_message},
    ]
    tools = _DriveTools()
    text = ""

    while True:
        allow_tools = tools.calls < MAX_TOOL_CALLS
        try:
            completion = _complete(messages, allow_tools)
        except LLMError as exc:
            if record:
                record(
                    CallRecord(
                        model=model_name(), latency_ms=exc.latency_ms, status=exc.kind
                    )
                )
            raise
        if record:
            record(completion.record)

        message = completion.message
        if not getattr(message, "tool_calls", None):
            text = (message.content or "").strip()
            break
        if not allow_tools:
            break  # the model ignored tool_choice="none"; fall back below

        messages.append(_assistant_message(message))
        for call in message.tool_calls:
            if tools.calls >= MAX_TOOL_CALLS:
                result = json.dumps(
                    {"error": "Tool limit reached. Answer with what you have."}
                )
            else:
                result = tools.run(call.function.name, call.function.arguments)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )

    if not text:
        text = "I couldn't put an answer together. Try rephrasing your question."

    sources = [s for link, s in tools.seen.items() if link in text]
    return CoachAnswer(
        text=text,
        sources=sources,
        drive_failed=tools.drive_failed,
        unsourced_numbers=has_unsourced_numbers(text, sources),
        tool_calls=tools.calls,
    )
