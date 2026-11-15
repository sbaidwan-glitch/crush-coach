"""Recommendation mode: the tools the coach may use when a member asks for one.

A member presses "Recommend" under an answer. The coach then gets two tools on top of
what it was already shown, and a longer time limit:

- `search_references`: the team's textbooks and reference documents in the Drive index
  (PDFs, and anything under a "References" folder). Each result has its title, place,
  date, a link and the passage, so the recommendation can cite a book the member can
  open. Every link it returns is registered as citable (`Retrieval.known`), so the
  answer check keeps it, and its `[R1]` number is turned into a link like `[S1]` is.
- `calculate`: arithmetic done by `calc.py`, not by the model.

Nothing here calls a model or touches Google: it reads the local index only (a short
database session per search, from `session_factory`). The tools are plain
`llm.Tool`s; `llm.CoachStream` runs them.
"""

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from sqlmodel import Session

import calc
import drive_index
import llm
import retrieval

REFERENCE_FOLDER_WORD = "reference"  # a folder path with this is a reference folder
PDF_RESULTS = 4  # textbook/handbook passages per search
REFERENCE_RESULTS = 3  # passages from documents in a "References" folder
EXCERPT_WORDS = 120
MAX_QUERY_CHARS = 200

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_references",
        "description": (
            "Search the team's reference books and reference documents (textbooks, "
            "handbooks, design notes in the References folders) for standard practice, "
            "formulas or recommended values. Use it to back a recommendation with a "
            "source the member can open. Search with the engineering terms "
            "(e.g. 'o-ring gland squeeze static face seal'), not the whole question. "
            "Results come numbered [R1], [R2]... with a link: cite them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to search for, up to a short sentence.",
                }
            },
            "required": ["query"],
        },
    },
}

CALCULATE_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": (
            "Do arithmetic exactly. Use it for EVERY number you derive; never calculate "
            "in your head. Pass one expression with plain numbers (units converted "
            "first), using + - * / ** (), pi, sqrt(), sin(), cos() (radians), etc. "
            "Show the formula and inputs in your answer next to the result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "For example: 0.030 / 0.050 or pi * (0.25 / 2) ** 2",
                },
                "what": {
                    "type": "string",
                    "description": "What this number is, e.g. 'gland depth in inches'.",
                },
            },
            "required": ["expression"],
        },
    },
}

SessionFactory = Callable[[], AbstractContextManager[Session]]


def _age_warning(hit: drive_index.SearchHit) -> str | None:
    """Why a result may be out of date, or None. A book isn't stale because it sits in
    an old season's folder (a handbook doesn't age with the season), but one in an
    OBSOLETE or Alfie folder still is."""
    reasons = [
        reason
        for reason in hit.old_reasons
        if not (hit.kind == "pdf" and "season" in reason)
    ]
    return "; ".join(reasons) or None


class RecommendKit:
    """The recommendation tools for one answer. `refs` fills in as the model searches:
    "R1" -> the passage, so `[R1]` in the answer can become a link."""

    def __init__(self, found: retrieval.Retrieval, session_factory: SessionFactory):
        self.found = found
        self.session_factory = session_factory
        self.refs: dict[str, drive_index.SearchHit] = {}

    def tools(self) -> list[llm.Tool]:
        return [
            llm.Tool(
                "search_references",
                SEARCH_TOOL,
                self.search_references,
                "Searching the reference books…",
            ),
            llm.Tool("calculate", CALCULATE_TOOL, self.calculate, "Calculating…"),
        ]

    # --- search_references ---

    def _hits(self, query: str) -> list[drive_index.SearchHit]:
        with self.session_factory() as session:
            books = drive_index.search(
                session, query, limit=PDF_RESULTS, max_per_file=2, kind="pdf"
            )
            wider = drive_index.search(session, query, limit=60, max_per_file=2)
        notes = [
            hit
            for hit in wider
            if hit.kind in ("doc", "sheet", "pdf")
            and hit.chunk_id is not None
            and REFERENCE_FOLDER_WORD in (hit.folder_path or "").casefold()
        ][:REFERENCE_RESULTS]
        chosen: dict[int, drive_index.SearchHit] = {}
        for hit in [*books, *notes]:
            if hit.chunk_id is not None:
                chosen.setdefault(hit.chunk_id, hit)
        return list(chosen.values())

    def _number(self, hit: drive_index.SearchHit) -> str:
        """The [R#] of this passage, the same one if it was found before."""
        for ref, known in self.refs.items():
            if known.chunk_id == hit.chunk_id:
                return ref
        ref = f"R{len(self.refs) + 1}"
        self.refs[ref] = hit
        retrieval.register_source(self.found.known, hit)
        return ref

    def search_references(self, arguments: dict[str, Any], seconds_left: float) -> str:
        query = str(arguments.get("query") or "").strip()[:MAX_QUERY_CHARS]
        if not query:
            return json.dumps({"error": "Give a query: the terms to search for."})
        words = retrieval.content_words(query)
        results = []
        for hit in self._hits(query):
            results.append(
                {
                    "ref": self._number(hit),
                    "title": hit.name,
                    "kind": hit.kind,
                    "folder": hit.folder_path or "unknown",
                    "modified": hit.modified_time.date().isoformat()
                    if hit.modified_time
                    else "unknown",
                    "possibly_old": _age_warning(hit),
                    "section": hit.heading,
                    "link": hit.web_link or None,
                    "passage": retrieval.excerpt(hit.text, words, EXCERPT_WORDS),
                }
            )
        if not results:
            return json.dumps(
                {
                    "results": [],
                    "note": "Nothing in the reference books matched. Say so; do not "
                    "invent a source. Any general advice must be labelled as such.",
                }
            )
        return json.dumps({"results": results}, ensure_ascii=False)

    # --- calculate ---

    def calculate(self, arguments: dict[str, Any], seconds_left: float) -> str:
        expression = str(arguments.get("expression") or "")
        try:
            value = calc.evaluate(expression)
        except calc.CalcError as exc:
            return json.dumps({"expression": expression, "error": str(exc)})
        return json.dumps(
            {
                "what": str(arguments.get("what") or ""),
                "expression": expression,
                "result": calc.format_result(value),
            }
        )
