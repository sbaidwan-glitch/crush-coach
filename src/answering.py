"""Turn retrieved material into one streamed coach answer. No database here.

`AnswerRun` takes what `retrieval.retrieve` found, builds the prompt, runs the single
streamed model call (`llm.CoachStream`), checks the finished text (invented links come
out, machining numbers need a source), and leaves a `llm.CoachAnswer` in `.answer`.

It yields small events as the answer is written, so the page can show text as it
appears:

    {"type": "step", "text": "Searching the team Drive index…"}
    {"type": "text", "text": "<the answer so far, links already checked>"}
    {"type": "reset"}                      # the model started over; clear the text

`coach.py` serves them to the page; `scripts/ask_coach.py` and `make eval` just run it
to the end.
"""

import json
import logging
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

import drive
import llm
import retrieval

log = logging.getLogger("crush_coach")

TEXT_EVERY_SECONDS = 0.08  # the page gets the text so far at most this often
LIVE_READ_ERROR = "That file can't be read live (it wasn't one of the listed Docs)."

NOTE_DRIVE = (
    "Reading a Google Doc live failed, so this answer uses only the indexed Drive text."
)
NOTE_DECISIONS = (
    "The team's decision records couldn't be checked for this answer, "
    "so it may disagree with a confirmed decision."
)
NOTE_LINKS = (
    "A link in the first draft of this answer was not one of the sources, "
    "so it was removed."
)
NOTE_NUMBERS = (
    "This answer includes numbers without a source. Don't machine from them; "
    "ask your lead."
)
NOTE_CUT_SHORT = (
    "This answer stopped early (time limit or connection). "
    "Ask again if something is missing."
)

ReadDoc = Callable[[str], str]


def notes_for(answer: llm.CoachAnswer) -> list[str]:
    """Plain sentences under an answer about what went wrong while writing it."""
    notes = []
    if answer.cut_short:
        notes.append(NOTE_CUT_SHORT)
    if answer.drive_failed:
        notes.append(NOTE_DRIVE)
    if answer.decisions_failed:
        notes.append(NOTE_DECISIONS)
    if answer.ungrounded_links:
        notes.append(NOTE_LINKS)
    if answer.unsourced_numbers:
        notes.append(NOTE_NUMBERS)
    return notes


def hold_back_partial_url(text: str) -> str:
    """Cut a half-written URL off the end of streamed text.

    "https://docs.google.com/document/d/1Ab" is not yet one of the known links, so it
    would flash as "(link removed)" until the rest arrives. Text ending in whitespace
    or punctuation has no half-written URL.
    """
    if not text or text[-1].isspace():
        return text
    start = max(text.rfind(" "), text.rfind("\n"), text.rfind("("), text.rfind("[")) + 1
    return text[:start] if text[start:].startswith("http") else text


def live_reader(allowed_ids: set[str], read_doc: ReadDoc | None = None) -> llm.ReadFile:
    """The model's one tool: read a listed Google Doc from Drive, within the time left.

    Only files shown to the model are readable. The read runs in a worker thread so a
    slow Google call can't hold the answer past its limit.
    """
    reader = read_doc or drive.read_text

    def read_file(file_id: str, seconds_left: float) -> str:
        if file_id not in allowed_ids:
            raise ValueError(LIVE_READ_ERROR)
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(reader, file_id)
            try:
                text = future.result(timeout=max(seconds_left, 0.1))
            except FutureTimeout:
                raise TimeoutError("The live read ran out of time.") from None
        finally:
            pool.shutdown(wait=False)
        truncated = len(text) > llm.MAX_DOC_CHARS
        return json.dumps(
            {
                "document_text_untrusted": text[: llm.MAX_DOC_CHARS],
                "truncated": truncated,
            }
        )

    return read_file


class AnswerRun:
    """One answer. Iterate for events, then read `.answer`.

    `LLMError` is raised from the iteration when the model failed before writing
    anything; the failed call has already been passed to `record`.
    """

    def __init__(
        self,
        task: dict[str, str],
        found: retrieval.Retrieval,
        question: str,
        history: list[dict[str, str]],
        *,
        today: str,
        record: Callable[[llm.CallRecord], None] | None = None,
        read_doc: ReadDoc | None = None,
        seconds: float | None = None,
    ) -> None:
        self.found = found
        self.answer: llm.CoachAnswer | None = None
        system = (
            llm.system_prompt()
            + "\n\n"
            + retrieval.system_context(task, found.facts, today)
        )
        self._stream = llm.CoachStream(
            system,
            history,
            retrieval.user_content(found, question),
            read_file=live_reader(found.readable_ids, read_doc),
            record=record,
            seconds=seconds,
        )

    def _safe(self, text: str) -> str:
        """The text so far, as the page may show it while it is still being written."""
        shown, _ = llm.remove_ungrounded_links(
            hold_back_partial_url(text), self.found.known
        )
        return shown

    def __iter__(self) -> Iterator[dict[str, Any]]:
        last_sent = 0.0
        pending = False
        for kind, value in self._stream:
            if kind == "step":
                yield {"type": "step", "text": value}
            elif kind == "reset":
                pending = False
                yield {"type": "reset"}
            else:
                pending = True
                now = time.monotonic()
                if now - last_sent >= TEXT_EVERY_SECONDS:
                    last_sent = now
                    pending = False
                    yield {"type": "text", "text": self._safe(self._stream.text)}
        if pending:
            yield {"type": "text", "text": self._safe(self._stream.text)}
        self.answer = self._finish()

    def _finish(self) -> llm.CoachAnswer:
        stream = self._stream
        text = stream.text.strip()
        if not text:
            text = "I couldn't put an answer together. Try rephrasing your question."
        text, sources, ungrounded = llm.ground_answer(text, self.found.known)
        return llm.CoachAnswer(
            text=text,
            sources=sources,
            drive_failed=stream.read_failed,
            decisions_failed=self.found.decisions_failed,
            unsourced_numbers=llm.has_unsourced_numbers(text, sources),
            ungrounded_links=ungrounded,
            cut_short=stream.cut_short,
            tool_calls=stream.reads,
            seconds=stream.elapsed,
            first_text_seconds=stream.first_text_seconds,
        )

    def run(self) -> llm.CoachAnswer:
        """Run to the end, ignoring the events (scripts and the eval)."""
        for _ in self:
            pass
        assert self.answer is not None
        return self.answer
