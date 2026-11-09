"""Settings (operational knobs, read from env each run) and Rules (survival
terms, frozen into life.json at birth so they cannot drift mid-life)."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping

MICROS = 1_000_000  # all money is integer micro-USD


def usd_to_micros(usd: float) -> int:
    return int(round(usd * MICROS))


def fmt_usd(micros: int) -> str:
    sign = "-" if micros < 0 else ""
    return f"{sign}${abs(micros) / MICROS:,.4f}"


@dataclass(frozen=True)
class Rules:
    """The terms of life. Enforced by code, never by the model."""

    seed_usd: float = 10.0  # starting compute allowance ("energy")
    first_revenue_deadline_h: float = 24.0  # must make a first sale within this
    max_drought_h: float = 24.0  # after that, max gap between qualifying sales
    min_sale_usd: float = 1.0  # a sale smaller than this (gross) doesn't count as "earning"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Rules":
        env = _nonempty(env)
        d = cls()
        return cls(
            seed_usd=float(env.get("EARNY_SEED_USD", d.seed_usd)),
            first_revenue_deadline_h=float(env.get("EARNY_DEADLINE_H", d.first_revenue_deadline_h)),
            max_drought_h=float(env.get("EARNY_DROUGHT_H", d.max_drought_h)),
            min_sale_usd=float(env.get("EARNY_MIN_SALE_USD", d.min_sale_usd)),
        )

    @classmethod
    def from_dict(cls, d: Mapping) -> "Rules":
        return cls(**{k: float(v) for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)


def _nonempty(env: Mapping[str, str]) -> dict[str, str]:
    """CI passes unset variables as empty strings; treat those as absent."""
    return {k: v for k, v in env.items() if v.strip() != ""}


def _bool(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    root: Path
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    tick_budget_usd: float = 0.75  # hard cap on compute per wake-up
    max_api_calls_per_tick: int = 24
    web_max_uses_per_call: int = 5
    default_wake_h: float = 1.0
    site_url: str = ""
    github_repo: str = ""  # owner/name
    github_token: str = field(default="", repr=False)
    operator_login: str = ""
    excluded_emails: frozenset[str] = frozenset()
    settlement_currency: str = "usd"
    allow_test_money: bool = False
    stripe_key: str = field(default="", repr=False)
    resend_key: str = field(default="", repr=False)
    email_from: str = ""
    operator_email: str = ""

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def site_dir(self) -> Path:
        return self.root / "site"

    @property
    def notes_dir(self) -> Path:
        return self.root / "notes"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ, root: Path | None = None) -> "Settings":
        env = _nonempty(env)
        root = root or Path(env.get("EARNY_ROOT") or Path(__file__).resolve().parent.parent)
        repo = env.get("EARNY_GITHUB_REPO") or env.get("GITHUB_REPOSITORY", "")
        owner = repo.split("/")[0] if "/" in repo else ""
        site = env.get("EARNY_SITE_URL", "")
        if not site and "/" in repo:
            o, n = repo.split("/", 1)
            site = f"https://{o.lower()}.github.io/{n}/"
        excluded = {e.strip().lower() for e in env.get("EARNY_EXCLUDED_EMAILS", "").split(",") if e.strip()}
        op_email = env.get("EARNY_OPERATOR_EMAIL", "").strip().lower()
        if op_email:
            excluded.add(op_email)
        d = cls(root=root)
        return cls(
            root=root,
            model=env.get("EARNY_MODEL", d.model),
            effort=env.get("EARNY_EFFORT", d.effort),
            tick_budget_usd=float(env.get("EARNY_TICK_BUDGET_USD", d.tick_budget_usd)),
            max_api_calls_per_tick=int(env.get("EARNY_MAX_CALLS_PER_TICK", d.max_api_calls_per_tick)),
            web_max_uses_per_call=int(env.get("EARNY_WEB_MAX_USES", d.web_max_uses_per_call)),
            default_wake_h=float(env.get("EARNY_DEFAULT_WAKE_H", d.default_wake_h)),
            site_url=site,
            github_repo=repo,
            github_token=env.get("GITHUB_TOKEN", ""),
            operator_login=env.get("EARNY_OPERATOR_LOGIN", owner),
            excluded_emails=frozenset(excluded),
            settlement_currency=env.get("EARNY_CURRENCY", d.settlement_currency).lower(),
            allow_test_money=_bool(env.get("EARNY_ALLOW_TEST_MONEY")),
            stripe_key=env.get("STRIPE_API_KEY", ""),
            resend_key=env.get("RESEND_API_KEY", ""),
            email_from=env.get("EARNY_EMAIL_FROM", ""),
            operator_email=op_email,
        )
