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
import re
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

import drive
import llm
import plan as plan_shape
import recommend as recommend_tools
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
NOTE_SHORTENED = (
    "This answer hit the length limit, so it ends at its last full sentence "
    "(shortened). Ask a follow-up for the rest."
)
NOTE_NO_SEARCH = (
    "Searching the reference books failed, so this recommendation uses only the "
    "team's own sources."
)
NOTE_NO_CALC = "The calculator failed, so check every number in this answer by hand."
NOTE_PLAN_NUMBERS = (
    "The “You should end up with” part of this plan has numbers in it, and it shouldn't. "
    "Check them against a source before you use them."
)
NOTE_CUT_SHORT = (
    "This answer stopped early (time limit or connection). "
    "Ask again if something is missing."
)

ReadDoc = Callable[[str], str]


def notes_for(answer: llm.CoachAnswer) -> list[str]:
    """Plain sentences under an answer about what went wrong while writing it."""
    notes = []
    if answer.shortened:
        notes.append(NOTE_SHORTENED)
    if answer.cut_short:
        notes.append(NOTE_CUT_SHORT)
    if answer.drive_failed:
        notes.append(NOTE_DRIVE)
    if "search_references" in answer.tools_failed:
        notes.append(NOTE_NO_SEARCH)
    if "calculate" in answer.tools_failed:
        notes.append(NOTE_NO_CALC)
    if answer.decisions_failed:
        notes.append(NOTE_DECISIONS)
    if answer.ungrounded_links:
        notes.append(NOTE_LINKS)
    if answer.unsourced_numbers:
        notes.append(NOTE_NUMBERS)
    if answer.plan_numbers:
        notes.append(NOTE_PLAN_NUMBERS)
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


_SENTENCE_END = re.compile(r"""[.!?]["')\]]*\**(?=\s|$)""")
_LINK = re.compile(r"\[[^\]\n]*\]\([^)\s]*\)")
_NOT_AN_END = ("e.g", "i.e", "vs", "approx", "fig", "no")


def clean_end(text: str) -> str:
    """Cut text that stopped mid-sentence back to its last full sentence.

    Never leaves half a Markdown link or an open **bold** behind, and does not take
    the "." of a list number ("2.") or of "e.g." for the end of a sentence. If no
    sentence is complete at all, keeps the text up to its last whole word and adds "…".
    """
    text = text.rstrip()
    links = [m.span() for m in _LINK.finditer(text)]
    open_link = max(text.rfind("["), text.rfind("]("))
    if open_link > max((end for _, end in links), default=-1):  # a link left open
        text = text[:open_link].rstrip()
        links = [m.span() for m in _LINK.finditer(text)]

    def inside_link(position: int) -> bool:
        return any(start < position < end for start, end in links)

    cut = 0
    for match in _SENTENCE_END.finditer(text):
        before = text[: match.start()]
        line_start = before.rfind("\n") + 1
        word = re.search(r"(\S+)$", before)
        if (
            inside_link(match.start())
            or before[line_start:].strip().isdigit()  # "2." starting a list item
            or (word and word.group(1).casefold().strip("*(") in _NOT_AN_END)
        ):
            continue
        cut = match.end()
    if cut:
        text = text[:cut].rstrip()
    else:
        text = text.rsplit(None, 1)[0] + "…" if " " in text else text
    if text.count("**") % 2:
        text += "**"
    return text


_SOURCE_REF = re.compile(r"\[([SR]\d+)\](?!\()")


def link_source_numbers(text: str, numbered: dict[str, Any]) -> str:
    """Turn a bare "[S3]" (the number the model saw in the prompt) or "[R2]" (a number a
    search tool gave it) into the Markdown link `[Title](link)` to that source.

    The prompt asks for links, but smaller models cite by number, and the page
    can't show or open a number. Only the links the model was given are used, so
    this can't plant a new one; a number that matches nothing is left as written.
    """

    def link(match: re.Match[str]) -> str:
        hit = numbered.get(match.group(1))
        if hit is None or not hit.web_link:
            return match.group(0)
        title = hit.name.replace("[", "(").replace("]", ")")
        return f"[{title}]({hit.web_link})"

    return _SOURCE_REF.sub(link, text)


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
        recommend: recommend_tools.RecommendKit | None = None,
        plan: bool = False,
    ) -> None:
        self.found = found
        self.numbered = retrieval.numbered_sources(found)
        self.recommend = recommend
        self.plan = plan
        self.answer: llm.CoachAnswer | None = None
        system = llm.system_prompt()
        if recommend is not None:
            system += llm.recommend_prompt()
        if plan:
            system += llm.plan_prompt()
        system += "\n\n" + retrieval.system_context(task, found.facts, today)
        if plan:  # one short call over the same retrieval: no tools, no live read
            self._stream = llm.CoachStream(
                system,
                history,
                retrieval.user_content(found, question),
                record=record,
                seconds=seconds,
                max_tokens=llm.PLAN_MAX_TOKENS,
            )
        elif recommend is None:
            self._stream = llm.CoachStream(
                system,
                history,
                retrieval.user_content(found, question),
                read_file=live_reader(found.readable_ids, read_doc),
                record=record,
                seconds=seconds,
            )
        else:  # a recommendation: more tools, more rounds, more time
            self._stream = llm.CoachStream(
                system,
                history,
                retrieval.user_content(found, question),
                read_file=live_reader(found.readable_ids, read_doc),
                tools=recommend.tools(),
                max_rounds=llm.RECOMMEND_ROUNDS,
                record=record,
                seconds=llm.RECOMMEND_SECONDS if seconds is None else seconds,
            )

    def _numbers(self) -> dict[str, Any]:
        """[S#] passages from the prompt, and [R#] ones the search tool found since."""
        return {**self.numbered, **(self.recommend.refs if self.recommend else {})}

    def _safe(self, text: str) -> str:
        """The text so far, as the page may show it while it is still being written."""
        shown, _ = llm.remove_ungrounded_links(
            link_source_numbers(hold_back_partial_url(text), self._numbers()),
            self.found.known,
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
        # `CoachStream` raises when there is no text at all, so there always is some.
        text = stream.text.strip()
        if stream.truncated or stream.cut_short:
            text = clean_end(text)
        text = link_source_numbers(text, self._numbers())
        text, sources, ungrounded = llm.ground_answer(text, self.found.known)
        return llm.CoachAnswer(
            text=text,
            sources=sources,
            drive_failed=stream.read_failed,
            decisions_failed=self.found.decisions_failed,
            unsourced_numbers=llm.has_unsourced_numbers(text, sources),
            plan_numbers=self.plan and plan_shape.has_numbers_in_outputs(text),
            ungrounded_links=ungrounded,
            cut_short=stream.cut_short,
            shortened=stream.truncated,
            tool_calls=stream.reads,
            tools_used=dict(stream.tools_used),
            tools_failed=list(stream.failed_tools),
            seconds=stream.elapsed,
            first_text_seconds=stream.first_text_seconds,
        )

    def run(self) -> llm.CoachAnswer:
        """Run to the end, ignoring the events (scripts and the eval)."""
        for _ in self:
            pass
        assert self.answer is not None
        return self.answer
