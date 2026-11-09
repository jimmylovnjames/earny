"""One wake-up of thought: a manual tool-use loop that pays for every call
before making the next one. Manual (not the SDK tool runner) because the
budget gate has to sit between calls, and cost must be charged per response."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from .config import MICROS, Settings
from .pricing import cost_of
from .tools import TOOL_SPECS, EndTick, ToolContext, run_tool

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
DYNAMIC_WEB_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
                      "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5-1", "claude-fable-5"}
MIN_CALL_ESTIMATE = 30_000  # $0.03: assume at least this much for the next call
WRAP_UP_AT = 0.7  # warn the model when this share of the tick cap is spent


@dataclass
class TickReport:
    calls: int = 0
    spent_micros: int = 0
    stop: str = ""
    final_text: str = ""
    errors: list[str] = field(default_factory=list)


def server_tools(model: str, max_uses: int) -> list[dict]:
    if max_uses <= 0:
        return []
    if model in DYNAMIC_WEB_MODELS:
        return [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_uses}]
    return [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses},
            {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": max_uses}]


class Brain:
    def __init__(self, client: Any, settings: Settings, ctx: ToolContext,
                 charge: Callable[[int, dict], None], balance: Callable[[], int]):
        self.client = client
        self.s = settings
        self.ctx = ctx
        self.charge = charge  # records a compute cost in the ledger
        self.balance = balance  # current balance in micros

    def _request(self, system: str, messages: list) -> Any:
        kwargs: dict[str, Any] = dict(
            model=self.s.model,
            max_tokens=16000,
            system=[{"type": "text", "text": system}],
            messages=messages,
            tools=TOOL_SPECS + server_tools(self.s.model, self.s.web_max_uses_per_call),
            thinking={"type": "adaptive"},
            output_config={"effort": self.s.effort},
            cache_control={"type": "ephemeral"},
        )
        if self.s.model in FALLBACK_MODELS:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        return self.client.beta.messages.create(**kwargs)

    def run(self, system: str, wake_message: str) -> TickReport:
        rep = TickReport()
        cap = int(self.s.tick_budget_usd * MICROS)
        messages: list[dict] = [{"role": "user", "content": wake_message}]
        last_cost = 0
        warned = False
        for _ in range(self.s.max_api_calls_per_tick):
            estimate = max(int(last_cost * 1.5), MIN_CALL_ESTIMATE)
            if rep.spent_micros + estimate > cap:
                rep.stop = "tick_budget"
                break
            if self.balance() - estimate <= 0:
                rep.stop = "low_energy"
                break
            try:
                resp = self._request(system, messages)
            except anthropic.APIStatusError as e:
                rep.errors.append(f"api {e.status_code}: {getattr(e, 'message', e)}"[:300])
                rep.stop = "api_error"
                break
            except anthropic.APIConnectionError as e:
                rep.errors.append(f"connection: {e}"[:300])
                rep.stop = "api_error"
                break

            cost, breakdown = cost_of(resp.usage, self.s.model)
            breakdown["served_by"] = getattr(resp, "model", self.s.model)
            breakdown["stop_reason"] = resp.stop_reason
            self.charge(cost, breakdown)
            rep.calls += 1
            rep.spent_micros += cost
            last_cost = cost

            texts = [b.text for b in resp.content if getattr(b, "type", "") == "text" and b.text.strip()]
            if texts:
                rep.final_text = "\n".join(texts)
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "pause_turn":
                continue
            if resp.stop_reason == "refusal":
                rep.stop = "refusal"
                break
            tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if resp.stop_reason == "max_tokens" or not tool_uses:
                rep.stop = resp.stop_reason or "end_turn"
                break

            # run sleep last so parallel calls in the same turn still happen
            tool_uses.sort(key=lambda b: b.name == "sleep")
            results: list[dict] = []
            ended = False
            for tu in tool_uses:
                if ended:
                    text, err = "skipped: you are asleep", True
                else:
                    try:
                        text, err = run_tool(self.ctx, tu.name, dict(tu.input or {}))
                    except EndTick as e:
                        text, err, ended = str(e), False, True
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": text, "is_error": err})
            if ended:
                rep.stop = "slept"
                break
            if not warned and rep.spent_micros >= cap * WRAP_UP_AT:
                warned = True
                results.append({"type": "text", "text": (
                    f"[heart] {rep.spent_micros / cap:.0%} of this wake-up's compute cap is spent. "
                    "Finish what matters, update notes/plan.md if needed, then write your journal entry or sleep.")})
            messages.append({"role": "user", "content": results})
        else:
            rep.stop = "max_calls"
        return rep
