"""What thinking costs. Errs high: overcounting burn is safe, undercounting is
how an agent cheats death by accident.

USD per million tokens: (input, output, cache_read). Cache writes are billed
at 1.25x input (5-minute TTL) or 2x input (1-hour TTL).
"""

from __future__ import annotations

from typing import Any

PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.00),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
HAIKU_55_LONG = (0.50, 2.50, 0.05)  # prompts over 100K tokens
UNKNOWN = (10.0, 50.0, 1.00)  # most expensive known tier
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches


def _get(obj: Any, name: str, default: Any = 0) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        v = obj.get(name, default)
    else:
        v = getattr(obj, name, default)
    return default if v is None else v


def price_for(model: str | None, prompt_tokens: int = 0) -> tuple[float, float, float]:
    m = (model or "").strip()
    if m == "claude-haiku-5-5" and prompt_tokens > 100_000:
        return HAIKU_55_LONG
    return PRICES.get(m, UNKNOWN)


def _attempt_usd(u: Any, model: str | None) -> float:
    inp = int(_get(u, "input_tokens"))
    out = int(_get(u, "output_tokens"))
    cread = int(_get(u, "cache_read_input_tokens"))
    cwrite = int(_get(u, "cache_creation_input_tokens"))
    cc = _get(u, "cache_creation", None)
    w5 = int(_get(cc, "ephemeral_5m_input_tokens")) if cc is not None else 0
    w1h = int(_get(cc, "ephemeral_1h_input_tokens")) if cc is not None else 0
    if cc is None or (w5 + w1h) == 0:
        w5, w1h = cwrite, 0
    p_in, p_out, p_read = price_for(model, inp + cread + w5 + w1h)
    usd = (inp * p_in + out * p_out + cread * p_read + w5 * p_in * 1.25 + w1h * p_in * 2.0) / 1e6
    return usd


def cost_of(usage: Any, request_model: str) -> tuple[int, dict]:
    """Return (micro-USD, breakdown) for one Messages API response's usage."""
    iterations = _get(usage, "iterations", None) or []
    parts = []
    if iterations:
        for it in iterations:
            model = _get(it, "model", None) or request_model
            parts.append({"model": model, "type": _get(it, "type", "message"), "usd": _attempt_usd(it, model)})
    else:
        parts.append({"model": request_model, "type": "message", "usd": _attempt_usd(usage, request_model)})
    stu = _get(usage, "server_tool_use", None)
    searches = int(_get(stu, "web_search_requests"))
    total = sum(p["usd"] for p in parts) + searches * WEB_SEARCH_USD
    breakdown = {
        "input_tokens": int(_get(usage, "input_tokens")),
        "output_tokens": int(_get(usage, "output_tokens")),
        "cache_read_input_tokens": int(_get(usage, "cache_read_input_tokens")),
        "cache_creation_input_tokens": int(_get(usage, "cache_creation_input_tokens")),
        "web_searches": searches,
        "attempts": parts,
    }
    # round UP to the next micro-dollar
    micros = int(-(-total * 1_000_000 // 1))
    return micros, breakdown
