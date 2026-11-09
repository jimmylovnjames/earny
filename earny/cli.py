from __future__ import annotations

import argparse
import json
import sys

from .config import MICROS, Rules, Settings, fmt_usd
from .heart import AlreadyAlive, Heart
from .ledger import LedgerCorrupt


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="earny", description="An agent that must earn to survive.")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("birth", help="start a life (starts the clock)")
    b.add_argument("--name", default="Earny")
    b.add_argument("--reincarnate", action="store_true", help="archive the current life and start a new one")
    sub.add_parser("tick", help="one heartbeat (run hourly)")
    sub.add_parser("status", help="vitals and countdown")
    sub.add_parser("verify", help="verify the ledger hash chain")
    s = sub.add_parser("simulate", help="offline life simulation, no API calls")
    s.add_argument("--scenario", default="never-earns")
    s.add_argument("--hours", type=int, default=96)
    s.add_argument("--cost", type=float, default=0.30, help="USD per wake-up")
    s.add_argument("--seed", type=float, default=10.0)
    a = p.parse_args(argv)

    if a.cmd == "simulate":
        from .sim import simulate
        simulate(a.scenario, a.hours, a.cost, a.seed)
        return 0

    settings = Settings.from_env()
    heart = Heart(settings)
    try:
        if a.cmd == "birth":
            life = heart.birth(a.name, Rules.from_env(), reincarnate=a.reincarnate)
            print(f"born: {life.name} ({life.id}) at {life.born_at}; rules {json.dumps(life.rules)}")
        elif a.cmd == "tick":
            print(heart.tick())
            rep = heart.last_report
            if rep and rep.stop == "api_error":
                print(f"model API error: {rep.errors}", file=sys.stderr)
                return 4  # fail the run so the operator is notified; state is still committed
        elif a.cmd == "verify":
            life, ledger, _, _ = heart.vitals()
            print(f"ok: {len(ledger.entries())} entries, chain intact")
        elif a.cmd == "status":
            tomb = heart.store.tombstone()
            if tomb:
                print(json.dumps(tomb, indent=2))
                return 0
            life, _, v, verdict = heart.vitals()
            print(f"{life.name} ({life.id}) age {v.age_h:.1f}h, {life.thinking_ticks} wake-ups")
            print(f"  balance {fmt_usd(v.balance_micros)} = seed {fmt_usd(v.seed_micros)} + earned "
                  f"{fmt_usd(v.earned_micros)} - spent {fmt_usd(v.spent_micros)}")
            print(f"  qualifying sales: {len(v.sales)}" + (f", last {v.last_sale_at}" if v.last_sale_at else ""))
            print(f"  deadline {verdict.deadline.isoformat()} ({verdict.hours_left(v.now):.1f}h left), "
                  f"{'ALIVE' if verdict.alive else 'DEAD: ' + str(verdict.cause)}")
            print(f"  next wake {life.next_wake_at or 'now'}")
    except AlreadyAlive as e:
        print(e, file=sys.stderr)
        return 2
    except LedgerCorrupt as e:
        print(f"LEDGER CORRUPT, refusing to run: {e}", file=sys.stderr)
        return 3
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
