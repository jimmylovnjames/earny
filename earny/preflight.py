"""Check every external dependency with the real keys *before* the clock
starts. Birth is refused unless the required checks pass.

Costs about $0.02-0.05 of Anthropic usage (one tiny call with the full tool
set). That spend is yours, not the agent's: it happens before birth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .brain import build_request
from .config import Settings, fmt_usd
from .integrations import GitHub, IntegrationError
from .pricing import cost_of


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _anthropic(s: Settings, llm: Any) -> Check:
    try:
        if llm is None:
            import anthropic
            llm = anthropic.Anthropic()
        req = build_request(s, "You are running a connectivity check.",
                            [{"role": "user", "content": "Reply with the single word OK. Do not call any tools."}],
                            max_tokens=2048)
        resp = llm.beta.messages.create(**req)
        cost, _ = cost_of(resp.usage, s.model)
        text = " ".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return Check("anthropic", resp.stop_reason in ("end_turn", "tool_use"),
                     f"{s.model} (served by {resp.model}) stop={resp.stop_reason} said={text[:40]!r} cost={fmt_usd(cost)}")
    except Exception as e:
        return Check("anthropic", False, f"{type(e).__name__}: {str(e)[:300]}")


def _stripe(s: Settings, client: Any) -> list[Check]:
    if not s.stripe_key and client is None:
        return [Check("stripe", False, "STRIPE_API_KEY is not set")]
    if "_test_" in s.stripe_key and not s.allow_test_money:
        return [Check("stripe", False, "this is a TEST key: test money can't keep it alive. Use a restricted live key.")]
    if client is None:
        import stripe
        client = stripe.StripeClient(s.stripe_key, max_network_retries=2)
    since = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
    probes = [
        ("stripe: read charges+fees", lambda: client.v1.charges.list(
            params={"limit": 1, "created": {"gte": since}, "expand": ["data.balance_transaction", "data.payment_intent"]})),
        ("stripe: read payment links", lambda: client.v1.payment_links.list(params={"limit": 1})),
        ("stripe: read checkout sessions", lambda: client.v1.checkout.sessions.list(params={"limit": 1})),
        ("stripe: read products", lambda: client.v1.products.list(params={"limit": 1})),
    ]
    out = []
    for name, fn in probes:
        try:
            fn()
            out.append(Check(name, True, "ok"))
        except Exception as e:
            out.append(Check(name, False, f"{type(e).__name__}: {str(e)[:200]}"))
    out.append(Check("stripe: write products/prices/links", True,
                     "not probed (would create live objects); the first create_offer call will tell", required=False))
    return out


def _github(s: Settings) -> Check:
    try:
        gh = GitHub(s.github_repo, s.github_token, s.operator_login)
        gh.open_issues()
        return Check("github", True, f"{s.github_repo}, operator={s.operator_login}")
    except IntegrationError as e:
        return Check("github", False, str(e)[:300], required=False)


def run(s: Settings, llm: Any = None, stripe_client: Any = None) -> list[Check]:
    checks = [_anthropic(s, llm), *_stripe(s, stripe_client), _github(s)]
    checks.append(Check("email", bool(s.resend_key and s.email_from),
                        "configured" if (s.resend_key and s.email_from) else
                        "not configured: it can't deliver by email (RESEND_API_KEY + EARNY_EMAIL_FROM)", required=False))
    checks.append(Check("operator exclusion", bool(s.excluded_emails),
                        f"{len(s.excluded_emails)} email(s) excluded" if s.excluded_emails else
                        "EARNY_OPERATOR_EMAIL not set: your own payments would count", required=False))
    return checks


def report(checks: list[Check]) -> tuple[str, bool]:
    lines = []
    for c in checks:
        mark = "PASS" if c.ok else ("FAIL" if c.required else "warn")
        lines.append(f"[{mark}] {c.name}: {c.detail}")
    ok = all(c.ok for c in checks if c.required)
    lines.append("preflight: READY - safe to start the clock" if ok else "preflight: NOT READY - do not birth yet")
    return "\n".join(lines), ok
