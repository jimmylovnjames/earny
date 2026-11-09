"""Revenue verification. The agent never reports its own income: the heart
asks the payment processor directly, and only money that a stranger actually
paid through one of *this life's* offers counts.

Filters applied to every payment:
  - must be tagged with this life's id (set on every payment link it creates)
  - live mode only (test-mode money is fake) unless EARNY_ALLOW_TEST_MONEY=1
  - payer email not in the operator exclusion list (no self-funding)
  - settlement currency must match EARNY_CURRENCY
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .config import MICROS, Settings
from .ledger import Ledger

ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}
LIFE_KEY = "earny_life"


def minor_to_micros(amount: int, currency: str) -> int:
    return int(amount) * (MICROS if currency.lower() in ZERO_DECIMAL else MICROS // 100)


@dataclass(frozen=True)
class Payment:
    ref: str
    paid_at: datetime
    gross_micros: int  # after refunds
    net_micros: int  # after processor fees and refunds
    currency: str
    email: str | None = None
    note: str = ""


class RevenueSource(Protocol):
    name: str

    def payments_since(self, since: datetime, life_id: str) -> list[Payment]: ...


class RevenueUnavailable(RuntimeError):
    pass


class StripeRevenue:
    name = "stripe"

    def __init__(self, settings: Settings, client=None):
        key = settings.stripe_key
        if client is None:
            if not key:
                raise RevenueUnavailable("STRIPE_API_KEY is not set")
            if ("_test_" in key) and not settings.allow_test_money:
                raise RevenueUnavailable("refusing a Stripe *test* key: test-mode money cannot keep an agent alive "
                                         "(set EARNY_ALLOW_TEST_MONEY=1 for a rehearsal)")
            import stripe

            client = stripe.StripeClient(key, max_network_retries=3)
        self.client = client
        self.settings = settings

    def payments_since(self, since: datetime, life_id: str) -> list[Payment]:
        try:
            page = self.client.v1.charges.list(params={
                "created": {"gte": int(since.timestamp())},
                "limit": 100,
                "expand": ["data.balance_transaction", "data.payment_intent"],
            })
            charges = [c.to_dict() if hasattr(c, "to_dict") else dict(c) for c in page.auto_paging_iter()]
        except Exception as exc:  # network, auth, permissions
            raise RevenueUnavailable(f"stripe: {exc}") from exc

        out: list[Payment] = []
        for ch in charges:
            if not (ch.get("paid") and ch.get("status") == "succeeded"):
                continue
            if not ch.get("livemode") and not self.settings.allow_test_money:
                continue
            pi = ch.get("payment_intent")
            meta = (pi.get("metadata") if isinstance(pi, dict) else None) or {}
            if meta.get(LIFE_KEY) != life_id:
                continue
            bd = ch.get("billing_details") or {}
            email = (bd.get("email") or ch.get("receipt_email") or "").strip().lower() or None
            if email and email in self.settings.excluded_emails:
                continue
            bt = ch.get("balance_transaction")
            if not isinstance(bt, dict):
                continue  # not settled yet; will be picked up on a later tick
            cur = (bt.get("currency") or "").lower()
            if cur != self.settings.settlement_currency:
                continue
            amount = int(ch.get("amount") or 0)
            refunded = int(ch.get("amount_refunded") or 0)
            bt_amount = int(bt.get("amount") or 0)
            ratio = (bt_amount / amount) if amount else 1.0
            gross = round((amount - refunded) * ratio)
            net = int(bt.get("net") or 0) - round(refunded * ratio)
            out.append(Payment(
                ref=ch["id"],
                paid_at=datetime.fromtimestamp(int(ch["created"]), tz=timezone.utc),
                gross_micros=minor_to_micros(gross, cur),
                net_micros=minor_to_micros(net, cur),
                currency=cur,
                email=email,
                note=(ch.get("description") or "")[:120],
            ))
        return out


class FileRevenue:
    """Payments from a JSON file. For simulations and tests only."""

    name = "file"

    def __init__(self, path: Path, excluded_emails: frozenset[str] = frozenset()):
        self.path = path
        self.excluded = excluded_emails

    def payments_since(self, since: datetime, life_id: str) -> list[Payment]:
        if not self.path.exists():
            return []
        out = []
        for p in json.loads(self.path.read_text()):
            if p.get("life", life_id) != life_id:
                continue
            if (p.get("email") or "").lower() in self.excluded:
                continue
            at = datetime.fromisoformat(p["paid_at"])
            if at >= since:
                out.append(Payment(p["ref"], at, int(p["gross_micros"]), int(p["net_micros"]),
                                   p.get("currency", "usd"), p.get("email")))
        return out


def sync_revenue(ledger: Ledger, source: RevenueSource, life_id: str, born_at: datetime, now: datetime) -> list[str]:
    """Bring the ledger in line with what the processor says. Idempotent.
    Returns human-readable lines describing new money (or clawbacks)."""
    recorded: dict[str, tuple[int, int]] = {}
    for e in ledger.entries():
        if e.kind in ("revenue", "revenue_adjustment") and e.ref:
            g, n = recorded.get(e.ref, (0, 0))
            recorded[e.ref] = (g + int(e.meta.get("gross_micros", 0)), n + e.amount_micros)
    news = []
    for p in sorted(source.payments_since(born_at, life_id), key=lambda p: p.paid_at):
        if p.ref not in recorded:
            ledger.append("revenue", p.net_micros, now, ref=p.ref, meta={
                "gross_micros": p.gross_micros, "paid_at": p.paid_at.isoformat(), "source": source.name,
                "currency": p.currency})
            news.append(f"+ sale {p.ref}: gross {p.gross_micros / MICROS:.2f}, net {p.net_micros / MICROS:.2f} {p.currency}")
        else:
            g, n = recorded[p.ref]
            dg, dn = p.gross_micros - g, p.net_micros - n
            if dg or dn:
                ledger.append("revenue_adjustment", dn, now, ref=p.ref, meta={
                    "gross_micros": dg, "reason": "refund_or_fee_change", "source": source.name})
                news.append(f"~ adjustment {p.ref}: net {dn / MICROS:+.2f}")
    return news
