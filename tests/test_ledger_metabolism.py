import json
from datetime import timedelta

import pytest

from earny.config import MICROS, Rules
from earny.ledger import Ledger, LedgerCorrupt
from earny.metabolism import compute_vitals, judge

from .conftest import T0

R = Rules(seed_usd=10, first_revenue_deadline_h=24, max_drought_h=24, min_sale_usd=1)


def born(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl")
    lg.append("birth", 10 * MICROS, T0)
    return lg


def sale(lg, ref, h, gross=5.0, net=4.55, at=None):
    paid = T0 + timedelta(hours=h)
    lg.append("revenue", int(net * MICROS), at or paid, ref=ref,
              meta={"gross_micros": int(gross * MICROS), "paid_at": paid.isoformat()})


def test_chain_detects_edits(tmp_path):
    lg = born(tmp_path)
    lg.append("compute", -500_000, T0)
    sale(lg, "ch_1", 1)
    lines = lg.path.read_text().splitlines()
    e = json.loads(lines[2])
    e["amount_micros"] = 999 * MICROS  # a hopeful edit
    lines[2] = json.dumps(e)
    lg.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerCorrupt, match="hash mismatch"):
        Ledger(lg.path).entries()


def test_chain_detects_deleted_line(tmp_path):
    lg = born(tmp_path)
    lg.append("compute", -1, T0)
    lg.append("compute", -1, T0)
    lines = lg.path.read_text().splitlines()
    lg.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    with pytest.raises(LedgerCorrupt):
        Ledger(lg.path).entries()


def test_never_earned_at_deadline(tmp_path):
    lg = born(tmp_path)
    assert judge(compute_vitals(lg.entries(), R, T0 + timedelta(hours=23.99)), R).alive
    v = judge(compute_vitals(lg.entries(), R, T0 + timedelta(hours=24)), R)
    assert not v.alive and v.cause == "never_earned"


def test_sale_resets_clock_then_drought(tmp_path):
    lg = born(tmp_path)
    sale(lg, "ch_1", 20)
    assert judge(compute_vitals(lg.entries(), R, T0 + timedelta(hours=43)), R).alive
    v = judge(compute_vitals(lg.entries(), R, T0 + timedelta(hours=44)), R)
    assert v.cause == "drought"


def test_starvation_beats_everything(tmp_path):
    lg = born(tmp_path)
    lg.append("compute", -10 * MICROS, T0 + timedelta(hours=1))
    v = judge(compute_vitals(lg.entries(), R, T0 + timedelta(hours=1)), R)
    assert v.cause == "starvation"


def test_small_sale_does_not_count_but_adds_energy(tmp_path):
    lg = born(tmp_path)
    sale(lg, "ch_tiny", 2, gross=0.5, net=0.2)
    vit = compute_vitals(lg.entries(), R, T0 + timedelta(hours=24))
    assert vit.balance_micros == int(10.2 * MICROS)
    assert judge(vit, R).cause == "never_earned"


def test_refund_unqualifies_sale(tmp_path):
    lg = born(tmp_path)
    sale(lg, "ch_1", 2)
    lg.append("revenue_adjustment", -int(4.85 * MICROS), T0 + timedelta(hours=3), ref="ch_1",
              meta={"gross_micros": -5 * MICROS})
    vit = compute_vitals(lg.entries(), R, T0 + timedelta(hours=24))
    assert vit.sales == () and judge(vit, R).cause == "never_earned"
    assert vit.earned_micros == int(-0.30 * MICROS)  # the fee is gone for good


def test_sale_timestamp_is_payment_time_not_sync_time(tmp_path):
    lg = born(tmp_path)
    sale(lg, "ch_1", 10, at=T0 + timedelta(hours=15))  # synced 5h late
    vit = compute_vitals(lg.entries(), R, T0 + timedelta(hours=34))
    assert judge(vit, R).cause == "drought"
