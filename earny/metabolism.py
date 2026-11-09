"""Vitals and the verdict. Pure functions of (ledger, rules, now).

Death causes, checked in this order:
  starvation    balance <= 0: it burned more compute than seed + earnings
  never_earned  no qualifying sale within first_revenue_deadline_h of birth
  drought       had sales, but none in the last max_drought_h hours
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Rules, usd_to_micros
from .ledger import Entry


@dataclass(frozen=True)
class Sale:
    ref: str
    paid_at: datetime
    gross_micros: int  # after refunds
    net_micros: int  # after fees and refunds


@dataclass(frozen=True)
class Vitals:
    born_at: datetime
    now: datetime
    seed_micros: int
    spent_micros: int  # positive number
    earned_micros: int  # net revenue, may be negative after refunds
    balance_micros: int
    sales: tuple[Sale, ...]  # qualifying sales only, oldest first
    last_sale_at: datetime | None
    burn_24h_micros: int
    dead: bool

    @property
    def age_h(self) -> float:
        return (self.now - self.born_at).total_seconds() / 3600


@dataclass(frozen=True)
class Verdict:
    alive: bool
    cause: str | None
    deadline: datetime  # when the timed rule kills it if nothing changes
    reason: str

    def hours_left(self, now: datetime) -> float:
        return max(0.0, (self.deadline - now).total_seconds() / 3600)


def compute_vitals(entries: list[Entry], rules: Rules, now: datetime) -> Vitals:
    if not entries or entries[0].kind != "birth":
        raise ValueError("ledger has no birth entry")
    born = entries[0].at
    seed = spent = earned = burn24 = 0
    per_ref: dict[str, dict] = {}
    for e in entries:
        if e.kind == "birth":
            seed += e.amount_micros
        elif e.kind == "compute":
            spent += -e.amount_micros
            if now - e.at <= timedelta(hours=24):
                burn24 += -e.amount_micros
        elif e.kind in ("revenue", "revenue_adjustment"):
            earned += e.amount_micros
            r = per_ref.setdefault(e.ref or f"anon-{e.seq}", {"gross": 0, "net": 0, "paid_at": None})
            r["gross"] += int(e.meta.get("gross_micros", 0))
            r["net"] += e.amount_micros
            if r["paid_at"] is None and e.meta.get("paid_at"):
                r["paid_at"] = datetime.fromisoformat(e.meta["paid_at"])
    min_gross = usd_to_micros(rules.min_sale_usd)
    sales = sorted(
        (Sale(ref, r["paid_at"], r["gross"], r["net"]) for ref, r in per_ref.items()
         if r["paid_at"] is not None and r["gross"] >= min_gross and r["paid_at"] >= born),
        key=lambda s: s.paid_at,
    )
    return Vitals(
        born_at=born,
        now=now,
        seed_micros=seed,
        spent_micros=spent,
        earned_micros=earned,
        balance_micros=seed + earned - spent,
        sales=tuple(sales),
        last_sale_at=sales[-1].paid_at if sales else None,
        burn_24h_micros=burn24,
        dead=any(e.kind == "death" for e in entries),
    )


def judge(v: Vitals, rules: Rules) -> Verdict:
    if v.last_sale_at is None:
        deadline = v.born_at + timedelta(hours=rules.first_revenue_deadline_h)
        timed_cause, timed_reason = "never_earned", (
            f"no qualifying sale (>= ${rules.min_sale_usd:.2f}) within "
            f"{rules.first_revenue_deadline_h:g}h of birth")
    else:
        deadline = v.last_sale_at + timedelta(hours=rules.max_drought_h)
        timed_cause, timed_reason = "drought", (
            f"no qualifying sale for {rules.max_drought_h:g}h (last at {v.last_sale_at.isoformat()})")

    if v.balance_micros <= 0:
        return Verdict(False, "starvation", deadline,
                       "balance hit zero: it burned more compute than it was given and earned")
    if v.now >= deadline:
        return Verdict(False, timed_cause, deadline, timed_reason)
    return Verdict(True, None, deadline, "alive")
