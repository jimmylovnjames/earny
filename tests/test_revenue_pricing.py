from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from earny.config import MICROS
from earny.ledger import Ledger
from earny.pricing import cost_of
from earny.revenue import RevenueUnavailable, StripeRevenue, sync_revenue

from .conftest import T0

LIFE = "earny-abc"


def charge(id, *, life=LIFE, live=True, email="a@b.c", amount=500, refunded=0, fee=45, cur="usd", settled=True,
           status="succeeded", h=1):
    return {
        "id": id, "paid": True, "status": status, "livemode": live, "created": int((T0 + timedelta(hours=h)).timestamp()),
        "amount": amount, "amount_refunded": refunded, "billing_details": {"email": email}, "receipt_email": None,
        "payment_intent": {"id": "pi", "metadata": {"earny_life": life} if life else {}},
        "balance_transaction": ({"amount": amount, "fee": fee, "net": amount - fee, "currency": cur}
                                if settled else "txn_pending"),
        "description": "",
    }


class FakeStripe:
    def __init__(self, charges):
        self.charges = charges
        page = SimpleNamespace(auto_paging_iter=lambda: iter(self.charges))
        self.v1 = SimpleNamespace(charges=SimpleNamespace(list=lambda params: page))


def src(settings, charges, **kw):
    return StripeRevenue(replace(settings, excluded_emails=frozenset({"op@me.com"}), **kw), client=FakeStripe(charges))


def test_filters(settings):
    s = src(settings, [
        charge("ok"),
        charge("other_business", life=None),
        charge("other_life", life="earny-zzz"),
        charge("test_mode", live=False),
        charge("operator", email="op@me.com"),
        charge("eur", cur="eur"),
        charge("unsettled", settled=False),
        charge("failed", status="failed"),
    ])
    assert [p.ref for p in s.payments_since(T0, LIFE)] == ["ok"]


def test_amounts_and_refunds(settings):
    [p] = src(settings, [charge("c", amount=1000, fee=59, refunded=400)]).payments_since(T0, LIFE)
    assert p.gross_micros == 6 * MICROS
    assert p.net_micros == (941 - 400) * 10_000


def test_rejects_test_key(settings):
    with pytest.raises(RevenueUnavailable, match="test"):
        StripeRevenue(replace(settings, stripe_key="rk_test_123"))


def test_sync_is_idempotent_and_tracks_refunds(settings, tmp_path):
    lg = Ledger(tmp_path / "l.jsonl")
    lg.append("birth", 10 * MICROS, T0)
    charges = [charge("c1")]
    s = src(settings, charges)
    assert len(sync_revenue(lg, s, LIFE, T0, T0)) == 1
    assert sync_revenue(lg, s, LIFE, T0, T0) == []
    charges[0] = charge("c1", refunded=500)
    news = sync_revenue(lg, s, LIFE, T0, T0)
    assert news and news[0].startswith("~")
    assert sum(e.amount_micros for e in lg.entries() if e.ref == "c1") == -45 * 10_000


def test_cost_simple():
    u = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 1_000_000,
         "cache_creation_input_tokens": 100_000, "server_tool_use": {"web_search_requests": 3}}
    micros, b = cost_of(u, "claude-opus-5-5")
    # 4 + 2 + 0.2 + 0.1*4*1.25 + 0.03
    assert micros == pytest.approx(6.73 * MICROS, abs=2)
    assert b["web_searches"] == 3


def test_cost_uses_iterations_with_fallback_model_prices():
    u = {"input_tokens": 0, "output_tokens": 0, "iterations": [
        {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1_000_000, "output_tokens": 0},
        {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1_000_000, "output_tokens": 0},
    ]}
    micros, _ = cost_of(u, "claude-opus-5-5")
    assert micros == 9 * MICROS


def test_unknown_model_priced_high():
    micros, _ = cost_of({"input_tokens": 1_000_000, "output_tokens": 0}, "claude-mystery-9")
    assert micros == 10 * MICROS


def test_empty_env_values_fall_back_to_defaults(tmp_path):
    from earny.config import Rules, Settings
    env = {"EARNY_MODEL": "", "EARNY_SEED_USD": " ", "GITHUB_REPOSITORY": "Me/earny", "EARNY_OPERATOR_EMAIL": "Op@Me.com"}
    s = Settings.from_env(env, root=tmp_path)
    assert s.model == "claude-opus-5-5" and Rules.from_env(env).seed_usd == 10.0
    assert s.site_url == "https://me.github.io/earny/" and s.operator_login == "Me"
    assert "op@me.com" in s.excluded_emails
