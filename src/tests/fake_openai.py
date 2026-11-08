"""A scripted stand-in for the OpenAI client: no network, no key."""

import copy
from types import SimpleNamespace

import httpx
import openai

_REQUEST = httpx.Request("POST", "https://litellm.example.test/v1/chat/completions")


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=_REQUEST)


def reply(text, input_tokens=100, output_tokens=20, cost="0.0012"):
    """A final answer."""
    return {
        "message": SimpleNamespace(content=text, tool_calls=None),
        "usage": SimpleNamespace(
            prompt_tokens=input_tokens, completion_tokens=output_tokens
        ),
        "headers": {"x-litellm-response-cost": cost} if cost else {},
    }


def tool_request(*calls, input_tokens=80, output_tokens=15, cost="0.0008"):
    """The model asks for tools. Each call is (id, name, arguments_json)."""
    tool_calls = [
        SimpleNamespace(
            id=call_id,
            type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        )
        for call_id, name, arguments in calls
    ]
    return {
        "message": SimpleNamespace(content=None, tool_calls=tool_calls),
        "usage": SimpleNamespace(
            prompt_tokens=input_tokens, completion_tokens=output_tokens
        ),
        "headers": {"x-litellm-response-cost": cost} if cost else {},
    }


def budget_exceeded():
    return openai.BadRequestError(
        "Budget has been exceeded! Current cost: 5.1, Max budget: 5.0",
        response=_response(400),
        body={"error": {"type": "budget_exceeded", "message": "Budget exceeded"}},
    )


def rate_limited():
    return openai.RateLimitError(
        "Rate limit reached", response=_response(429), body={"error": "rate"}
    )


def timed_out():
    return openai.APITimeoutError(request=_REQUEST)


def unreachable():
    return openai.APIConnectionError(request=_REQUEST)


def key_rejected():
    return openai.AuthenticationError(
        "bad key sk-secret-123", response=_response(401), body=None
    )


def server_error():
    return openai.InternalServerError(
        "boom sk-secret-123", response=_response(500), body=None
    )


class _Raw:
    def __init__(self, spec):
        self._spec = spec
        self.headers = spec["headers"]

    def parse(self):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=self._spec["message"])],
            usage=self._spec["usage"],
        )


class FakeClient:
    """Plays `script` in order: dicts are responses, exceptions are raised."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        raw = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(completions=SimpleNamespace(with_raw_response=raw))

    def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        if not self.script:
            raise AssertionError("the model was called more times than scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Raw(item)
