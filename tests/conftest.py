from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from anthropic.types.beta import BetaMessage

from earny.config import Settings

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings(tmp_path):
    return Settings(root=tmp_path, tick_budget_usd=0.50, site_url="https://x.github.io/earny/")


def msg(content: list[dict], stop_reason: str = "end_turn", input_tokens: int = 10_000, output_tokens: int = 1_000,
        model: str = "claude-opus-5-5", **usage) -> BetaMessage:
    return BetaMessage.model_validate({
        "id": "msg_x", "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, **usage},
    })


class FakeLLM:
    """Stands in for anthropic.Anthropic(); returns scripted responses and records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.requests.append(kw)
        if not self.responses:
            raise AssertionError("unexpected extra API call")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
