"""Persistent identity: who this agent is, when it was born, when it next wakes.
Lives in state/, which the agent's own tools cannot write."""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Rules


@dataclass
class Life:
    id: str
    name: str
    born_at: str
    rules: dict
    model: str
    next_wake_at: str | None = None
    ticks: int = 0
    thinking_ticks: int = 0
    last_tick_at: str | None = None
    seen: dict = field(default_factory=dict)  # watermarks: operator comments, orders

    @property
    def rules_obj(self) -> Rules:
        return Rules.from_dict(self.rules)

    @property
    def born(self) -> datetime:
        return datetime.fromisoformat(self.born_at)

    @classmethod
    def new(cls, name: str, rules: Rules, model: str, now: datetime) -> "Life":
        return cls(id=f"{name.lower()}-{secrets.token_hex(4)}", name=name, born_at=now.isoformat(),
                   rules=rules.to_dict(), model=model)


class Store:
    """Filesystem layout of one life."""

    def __init__(self, state_dir: Path):
        self.dir = state_dir

    @property
    def life_path(self) -> Path:
        return self.dir / "life.json"

    @property
    def ledger_path(self) -> Path:
        return self.dir / "ledger.jsonl"

    @property
    def tombstone_path(self) -> Path:
        return self.dir / "tombstone.json"

    @property
    def orders_path(self) -> Path:
        return self.dir / "orders.json"

    @property
    def log_path(self) -> Path:
        return self.dir / "heartbeat.log"

    def load_life(self) -> Life | None:
        if not self.life_path.exists():
            return None
        return Life(**json.loads(self.life_path.read_text()))

    def save_life(self, life: Life) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.life_path.write_text(json.dumps(asdict(life), indent=2, sort_keys=True) + "\n")

    def tombstone(self) -> dict | None:
        if not self.tombstone_path.exists():
            return None
        return json.loads(self.tombstone_path.read_text())

    def orders(self) -> dict:
        """order_id -> {"fulfilled_at": iso, "emails": n}. Never stores customer PII (repo is public)."""
        if not self.orders_path.exists():
            return {}
        return json.loads(self.orders_path.read_text())

    def save_orders(self, orders: dict) -> None:
        self.orders_path.write_text(json.dumps(orders, indent=2, sort_keys=True) + "\n")

    def log(self, line: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(line.rstrip() + "\n")
