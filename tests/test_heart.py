import json
from datetime import timedelta

import pytest

from earny.brain import TickReport
from earny.config import MICROS, Rules
from earny.heart import AlreadyAlive, Heart
from earny.ledger import Ledger
from earny.revenue import FileRevenue
from earny.sim import FakeBrain, simulate

from .conftest import T0


class RecordingBrain(FakeBrain):
    def __init__(self, cost_usd=0.2, sleep_h=None):
        super().__init__(cost_usd)
        self.wakes = []
        self.sleep_h = sleep_h

    def run(self, system, wake):
        self.wakes.append(wake)
        if self.sleep_h:
            self.ctx.sleep_until = self.ctx.now() + timedelta(hours=self.sleep_h)
        return super().run(system, wake)


def mk(settings, clock, brain=None):
    pay = settings.root / "payments.json"
    h = Heart(settings, now=clock, revenue=FileRevenue(pay), brain_factory=brain or RecordingBrain())
    return h, pay


def pay(pay_file, life, ref, at, gross=5.0):
    data = json.loads(pay_file.read_text()) if pay_file.exists() else []
    data.append({"ref": ref, "paid_at": at.isoformat(), "gross_micros": int(gross * MICROS),
                 "net_micros": int(gross * 0.9 * MICROS), "life": life.id})
    pay_file.write_text(json.dumps(data))


def test_birth_is_exclusive(settings, clock):
    h, _ = mk(settings, clock)
    h.birth("A", Rules())
    with pytest.raises(AlreadyAlive):
        h.birth("B", Rules())


def test_sleeping_costs_nothing_and_sale_wakes_it(settings, clock):
    brain = RecordingBrain(sleep_h=8)
    h, pay_file = mk(settings, clock, brain)
    life = h.birth("A", Rules())
    assert h.tick().startswith("thought")
    for hr in range(1, 4):
        clock.t = T0 + timedelta(hours=hr)
        assert h.tick().startswith("asleep")
    assert len(brain.wakes) == 1
    pay(pay_file, life, "ch_1", clock.t)
    clock.t += timedelta(hours=1)
    out = h.tick()
    assert out.startswith("thought (sale)"), out
    assert "+ sale ch_1" in brain.wakes[-1]


def test_rules_frozen_at_birth(settings, clock, monkeypatch):
    h, _ = mk(settings, clock)
    h.birth("A", Rules(first_revenue_deadline_h=24))
    monkeypatch.setenv("EARNY_DEADLINE_H", "9999")  # changing env later must not extend life
    clock.t = T0 + timedelta(hours=24)
    assert h.tick().startswith("DIED: never_earned")


def test_death_is_final_and_cleans_up(settings, clock):
    h, _ = mk(settings, clock)
    h.birth("Mortal", Rules())
    (settings.site_dir / "shop.html").write_text("buy now")
    clock.t = T0 + timedelta(hours=25)
    assert h.tick().startswith("DIED")
    tomb = h.store.tombstone()
    assert tomb["cause"] == "never_earned" and tomb["name"] == "Mortal"
    assert "Nothing here is for sale" in (settings.site_dir / "index.html").read_text()
    assert not (settings.site_dir / "shop.html").exists()
    assert (settings.state_dir / "remains" / "site" / "shop.html").exists()
    assert Ledger(h.store.ledger_path).entries()[-1].kind == "death"
    clock.t += timedelta(hours=1)
    assert h.tick().startswith("dead:")
    assert h.tick().startswith("dead:")


def test_reincarnation_archives_old_life(settings, clock):
    h, _ = mk(settings, clock)
    old = h.birth("One", Rules())
    clock.t = T0 + timedelta(hours=25)
    h.tick()
    new = h.birth("Two", Rules(), reincarnate=True)
    assert new.id != old.id
    assert (settings.state_dir / "graveyard" / old.id / "tombstone.json").exists()
    assert h.store.tombstone() is None


def test_last_chance_wake(settings, clock):
    brain = RecordingBrain(sleep_h=24)
    h, _ = mk(settings, clock, brain)
    h.birth("A", Rules())
    h.tick()
    clock.t = T0 + timedelta(hours=21, minutes=30)
    assert "last_chance" in h.tick()


@pytest.mark.parametrize("scenario,cause", [("never-earns", "never_earned"), ("one-hit-wonder", "drought"),
                                            ("late-bloomer", "starvation")])
def test_simulated_deaths(tmp_path, scenario, cause):
    assert simulate(scenario, hours=96, root=tmp_path, out=lambda *_: None)["cause"] == cause


def test_simulated_survival(tmp_path):
    assert simulate("steady", hours=80, root=tmp_path, out=lambda *_: None)["alive"]
