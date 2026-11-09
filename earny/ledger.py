"""Append-only, hash-chained money ledger.

Every unit of energy the agent gains or burns is one line here. Each entry
commits to the previous entry's hash, so silent edits (by the agent, a bad
merge, or a hopeful human) are detected and the heart refuses to beat.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
KINDS = {"birth", "compute", "revenue", "revenue_adjustment", "death"}


class LedgerCorrupt(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    seq: int
    ts: str  # ISO-8601 UTC
    kind: str
    amount_micros: int  # signed: + gains energy, - burns it
    ref: str | None
    meta: dict[str, Any]
    prev: str
    hash: str

    @property
    def at(self) -> datetime:
        return datetime.fromisoformat(self.ts)


def _digest(seq: int, ts: str, kind: str, amount: int, ref: str | None, meta: dict, prev: str) -> str:
    body = json.dumps(
        {"seq": seq, "ts": ts, "kind": kind, "amount_micros": amount, "ref": ref, "meta": meta, "prev": prev},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self._entries: list[Entry] | None = None

    def entries(self) -> list[Entry]:
        if self._entries is None:
            self._entries = self._load()
        return list(self._entries)

    def _load(self) -> list[Entry]:
        if not self.path.exists():
            return []
        out: list[Entry] = []
        prev = GENESIS
        for n, line in enumerate(self.path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                e = Entry(**raw)
            except (ValueError, TypeError) as exc:
                raise LedgerCorrupt(f"line {n}: unparseable ({exc})") from exc
            if e.seq != len(out):
                raise LedgerCorrupt(f"line {n}: seq {e.seq}, expected {len(out)}")
            if e.prev != prev:
                raise LedgerCorrupt(f"line {n}: broken chain")
            if e.kind not in KINDS:
                raise LedgerCorrupt(f"line {n}: unknown kind {e.kind!r}")
            if _digest(e.seq, e.ts, e.kind, e.amount_micros, e.ref, e.meta, e.prev) != e.hash:
                raise LedgerCorrupt(f"line {n}: hash mismatch (entry was edited)")
            out.append(e)
            prev = e.hash
        return out

    def append(self, kind: str, amount_micros: int, ts: datetime, ref: str | None = None, meta: dict | None = None) -> Entry:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        entries = self.entries()
        seq = len(entries)
        prev = entries[-1].hash if entries else GENESIS
        meta = json.loads(json.dumps(meta or {}, sort_keys=True))  # normalise
        ts_s = ts.isoformat()
        e = Entry(seq, ts_s, kind, int(amount_micros), ref, meta, prev,
                  _digest(seq, ts_s, kind, int(amount_micros), ref, meta, prev))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(e), sort_keys=True, separators=(",", ":")) + "\n")
        assert self._entries is not None
        self._entries.append(e)
        return e
