"""Offline life simulation: real heart, real ledger, real rules, fake clock,
fake brain (fixed cost per wake-up), scripted customers. Costs nothing.

    python -m earny simulate --scenario never-earns
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .brain import TickReport
from .config import MICROS, Rules, Settings, fmt_usd
from .heart import Heart
from .revenue import FileRevenue

SCENARIOS = {
    # hours after birth at which a $5 sale lands
    "never-earns": [],
    "one-hit-wonder": [10],
    "steady": [6, 20, 36, 50, 66],
    "late-bloomer": [23.5, 40],
}


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class FakeBrain:
    def __init__(self, cost_usd: float):
        self.cost = int(cost_usd * MICROS)

    def __call__(self, _llm, _settings, ctx, charge, balance):
        self.ctx, self.charge, self.balance = ctx, charge, balance
        return self

    def run(self, system: str, wake: str) -> TickReport:
        self.charge(self.cost, {"sim": True})
        return TickReport(calls=1, spent_micros=self.cost, stop="end_turn", final_text="(simulated thought)")


def simulate(scenario: str, hours: int = 96, cost_per_wake: float = 0.30, seed: float = 10.0,
             root: Path | None = None, out=print) -> dict:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario; choose from {sorted(SCENARIOS)}")
    root = root or Path(tempfile.mkdtemp(prefix="earny-sim-"))
    born = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = Clock(born)
    pay_file = root / "payments.json"
    settings = Settings(root=root, tick_budget_usd=1.0)
    heart = Heart(settings, now=clock, revenue=FileRevenue(pay_file), brain_factory=FakeBrain(cost_per_wake))
    life = heart.birth("Sim", replace(Rules(), seed_usd=seed))
    payments = [{"ref": f"ch_{i}", "paid_at": (born + timedelta(hours=h)).isoformat(), "gross_micros": 5 * MICROS,
                 "net_micros": int(4.55 * MICROS), "life": life.id} for i, h in enumerate(SCENARIOS[scenario])]
    out(f"scenario={scenario} seed=${seed:.2f} cost/wake=${cost_per_wake:.2f} sales at h={SCENARIOS[scenario]}")
    result = {}
    for h in range(hours + 1):
        clock.t = born + timedelta(hours=h)
        pay_file.write_text(json.dumps([p for p in payments if p["paid_at"] <= clock.t.isoformat()]))
        msg = heart.tick()
        if msg.startswith(("thought", "DIED")):
            out(f"  h={h:>3}  {msg}")
        if msg.startswith("DIED"):
            result = heart.store.tombstone() or {}
            break
    else:
        _, _, v, verdict = heart.vitals()
        result = {"alive": True, "balance_usd": v.balance_micros / MICROS, "sales": len(v.sales)}
        out(f"  survived {hours}h: balance {fmt_usd(v.balance_micros)}, {len(v.sales)} sales")
    return result
