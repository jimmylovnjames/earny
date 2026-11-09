"""The heartbeat. Runs once per scheduled tick (hourly). Order of operations:

  1. dead already? -> stop
  2. verify the ledger hash chain
  3. ask Stripe what was actually earned; write it to the ledger
  4. judge. dead -> die
  5. asleep and nothing new? -> stop (costs nothing)
  6. think: one budget-capped wake-up of the model, each call charged to the ledger
  7. judge again (it can starve mid-thought). dead -> die
"""

from __future__ import annotations

import html
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .brain import Brain, TickReport
from .config import MICROS, Rules, Settings, fmt_usd, usd_to_micros
from .integrations import GitHub, IntegrationError, Mailer, Shop
from .ledger import Ledger
from .life import Life, Store
from .metabolism import Verdict, Vitals, compute_vitals, judge
from .prompts import SYSTEM, WAKE
from .revenue import RevenueSource, RevenueUnavailable, StripeRevenue, sync_revenue
from .tools import ToolContext

WORKFLOW_FILE = "heartbeat.yml"
LAST_CHANCE_H = 3.0  # force a wake-up when this close to death


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class AlreadyAlive(RuntimeError):
    pass


class Heart:
    def __init__(self, settings: Settings, now: Callable[[], datetime] = utcnow, revenue: RevenueSource | None = None,
                 llm: Any = None, shop: Shop | None = None, github: GitHub | None = None, mailer: Mailer | None = None,
                 brain_factory: Callable[..., Any] | None = None):
        self.s = settings
        self.now = now
        self.store = Store(settings.state_dir)
        self._revenue = revenue
        self._llm = llm
        self._shop = shop
        self._github = github
        self._mailer = mailer
        self._brain_factory = brain_factory or Brain
        self.last_report: TickReport | None = None

    # ------------------------------------------------------------------ wiring

    def revenue(self) -> RevenueSource:
        if self._revenue is None:
            self._revenue = StripeRevenue(self.s)
        return self._revenue

    def shop(self, life: Life) -> Shop | None:
        if self._shop is None and self.s.stripe_key:
            import stripe
            self._shop = Shop(stripe.StripeClient(self.s.stripe_key, max_network_retries=3), life.id,
                              self.s.settlement_currency)
        return self._shop

    def github(self) -> GitHub | None:
        if self._github is None:
            try:
                self._github = GitHub(self.s.github_repo, self.s.github_token, self.s.operator_login)
            except IntegrationError:
                return None
        return self._github

    def mailer(self) -> Mailer | None:
        if self._mailer is None:
            try:
                self._mailer = Mailer(self.s.resend_key, self.s.email_from)
            except IntegrationError:
                return None
        return self._mailer

    def llm(self) -> Any:
        if self._llm is None:
            import anthropic
            self._llm = anthropic.Anthropic()
        return self._llm

    # ------------------------------------------------------------------ birth

    def birth(self, name: str, rules: Rules, reincarnate: bool = False) -> Life:
        existing = self.store.load_life()
        if existing and not reincarnate:
            raise AlreadyAlive(f"{existing.name} ({existing.id}) already exists; pass --reincarnate to archive it "
                               "and start a new, separate life")
        if existing:
            grave = self.s.state_dir / "graveyard" / existing.id
            grave.mkdir(parents=True, exist_ok=True)
            for p in self.s.state_dir.iterdir():
                if p.name != "graveyard":
                    shutil.move(str(p), str(grave / p.name))
            for d in (self.s.notes_dir, self.s.site_dir):
                if d.exists():
                    shutil.move(str(d), str(grave / d.name))
        now = self.now()
        life = Life.new(name, rules, self.s.model, now)
        self.store.save_life(life)
        Ledger(self.store.ledger_path).append("birth", usd_to_micros(rules.seed_usd), now,
                                             meta={"life": life.id, "rules": rules.to_dict()})
        self.s.notes_dir.mkdir(parents=True, exist_ok=True)
        self.s.site_dir.mkdir(parents=True, exist_ok=True)
        plan = self.s.notes_dir / "plan.md"
        if not plan.exists():
            plan.write_text("(empty - write your plan here)\n")
        idx = self.s.site_dir / "index.html"
        if not idx.exists():
            idx.write_text(f"<!doctype html><meta charset=utf-8><title>{html.escape(name)}</title>"
                           f"<p>{html.escape(name)} was just born. Something will be here soon.</p>\n")
        self.store.log(f"{now.isoformat()} BIRTH {life.id} seed={rules.seed_usd} rules={json.dumps(rules.to_dict())}")
        return life

    # ------------------------------------------------------------------ status

    def vitals(self) -> tuple[Life, Ledger, Vitals, Verdict]:
        life = self.store.load_life()
        if life is None:
            raise RuntimeError("not born yet: run `earny birth`")
        ledger = Ledger(self.store.ledger_path)
        v = compute_vitals(ledger.entries(), life.rules_obj, self.now())
        return life, ledger, v, judge(v, life.rules_obj)

    # ------------------------------------------------------------------ tick

    def tick(self) -> str:
        tomb = self.store.tombstone()
        if tomb:
            return f"dead: {tomb['name']} died {tomb['died_at']} ({tomb['cause']}). Nothing to do."
        life, ledger, _, _ = self.vitals()  # verifies the chain
        now = self.now()
        rules = life.rules_obj

        news: list[str] = []
        try:
            news += sync_revenue(ledger, self.revenue(), life.id, life.born, now)
        except RevenueUnavailable as e:
            news.append(f"! could not verify revenue this tick ({e}). Unverified money does not count.")
        v = compute_vitals(ledger.entries(), rules, now)
        verdict = judge(v, rules)
        if not verdict.alive:
            return self.die(life, ledger, v, verdict)

        # what's new in the world (cheap API calls, no model)
        gh = self.github()
        op_since = datetime.fromisoformat(life.seen.get("operator_at", life.born_at))
        operator: list[dict] = []
        if gh:
            try:
                operator = [m for m in gh.operator_messages(op_since) if datetime.fromisoformat(
                    m["at"].replace("Z", "+00:00")) > op_since]
            except IntegrationError as e:
                news.append(f"! could not read operator messages: {e}")
        shop = self.shop(life)
        orders_all = []
        if shop:
            try:
                orders_all = shop.orders(life.born)
            except Exception as e:
                news.append(f"! could not list orders: {e}")
        fulfilled = self.store.orders()
        pending = [o for o in orders_all if o.id not in fulfilled]
        last_order_seen = life.seen.get("order_at")
        new_orders = [o for o in pending if last_order_seen is None or o.created.isoformat() > last_order_seen]

        due = life.next_wake_at is None or now >= datetime.fromisoformat(life.next_wake_at)
        sales_news = [n for n in news if n.startswith(("+", "~"))]
        last_chance = verdict.hours_left(now) <= LAST_CHANCE_H and (
            life.last_tick_at is None or now - datetime.fromisoformat(life.last_tick_at) >= timedelta(hours=1))
        reasons = [r for r, on in (("scheduled", due), ("sale", bool(sales_news)), ("order", bool(new_orders)),
                                   ("operator", bool(operator)), ("last_chance", last_chance)) if on]
        life.ticks += 1
        if not reasons:
            self.store.save_life(life)
            self.store.log(f"{now.isoformat()} asleep balance={fmt_usd(v.balance_micros)} "
                           f"deadline_in={verdict.hours_left(now):.1f}h")
            return f"asleep until {life.next_wake_at}; {verdict.hours_left(now):.1f}h to deadline"

        rep = self.think(life, ledger, v, verdict, news, pending, operator, shop, now, reasons)

        if operator:
            life.seen["operator_at"] = operator[-1]["at"].replace("Z", "+00:00")
        if pending:
            life.seen["order_at"] = max(o.created for o in pending).isoformat()
        self.store.save_life(life)

        v = compute_vitals(ledger.entries(), rules, self.now())
        verdict = judge(v, rules)
        if not verdict.alive:
            return self.die(life, ledger, v, verdict)
        return (f"thought ({', '.join(reasons)}): {rep.calls} calls, {fmt_usd(rep.spent_micros)}, stop={rep.stop}; "
                f"balance {fmt_usd(v.balance_micros)}, {verdict.hours_left(self.now()):.1f}h to deadline")

    def think(self, life: Life, ledger: Ledger, v: Vitals, verdict: Verdict, news: list[str], pending: list,
              operator: list[dict], shop: Shop | None, now: datetime, reasons: list[str]) -> TickReport:
        rules = life.rules_obj
        life.thinking_ticks += 1
        ctx = ToolContext(settings=self.s, life=life, store=self.store, now=self.now, shop=shop,
                          github=self.github(), mailer=self.mailer())
        system = SYSTEM.format(name=life.name, seed=rules.seed_usd, min_sale=rules.min_sale_usd,
                               deadline_h=rules.first_revenue_deadline_h, drought_h=rules.max_drought_h)
        wake = self.wake_message(life, v, verdict, news, pending, operator, shop, now, reasons)

        def charge(cost: int, meta: dict) -> None:
            ledger.append("compute", -cost, self.now(), meta=meta)

        def balance() -> int:
            return compute_vitals(ledger.entries(), rules, self.now()).balance_micros

        brain = self._brain_factory(self.llm(), self.s, ctx, charge, balance)
        rep: TickReport = brain.run(system, wake)
        self.last_report = rep

        life.next_wake_at = (ctx.sleep_until or (now + timedelta(hours=self.s.default_wake_h))).isoformat()
        life.last_tick_at = now.isoformat()
        entry = (f"\n## #{life.thinking_ticks} - {now.isoformat()} - spent {fmt_usd(rep.spent_micros)} "
                 f"in {rep.calls} calls ({rep.stop})\n\n{rep.final_text.strip() or '(no journal entry)'}\n")
        if ctx.actions:
            entry += "\nactions: " + "; ".join(ctx.actions) + "\n"
        with (self.s.notes_dir / "journal.md").open("a") as f:
            f.write(entry)
        self.store.log(f"{now.isoformat()} think reasons={','.join(reasons)} calls={rep.calls} "
                       f"spent={fmt_usd(rep.spent_micros)} stop={rep.stop} errors={rep.errors}")
        return rep

    def wake_message(self, life: Life, v: Vitals, verdict: Verdict, news: list[str], pending: list,
                     operator: list[dict], shop: Shop | None, now: datetime, reasons: list[str]) -> str:
        burn_h = v.burn_24h_micros / max(1.0, min(24.0, v.age_h))
        runway = f"{v.balance_micros / burn_h:.0f}h" if burn_h > 0 else "n/a (no burn yet)"
        if pending:
            orders = "\n".join(
                f"- {o.id} | {o.created.isoformat()} | {o.offer} | {o.amount_micros / MICROS:.2f} {o.currency}\n"
                f"  <customer_request untrusted>{o.request or '(none)'}</customer_request>" for o in pending)
        else:
            orders = "(none)"
        offers = "(stripe not configured: you cannot sell until the operator adds STRIPE_API_KEY)"
        if shop:
            try:
                ol = shop.offers(active_only=False)
                offers = "\n".join(f"- {o['id']} {'ACTIVE' if o['active'] else 'inactive'} {o['name']} {o['url']}"
                                   for o in ol) or "(none yet)"
            except Exception as e:
                offers = f"(could not list: {e})"
        site_files = "\n".join(f"  {p.relative_to(self.s.root)}" for p in sorted(self.s.site_dir.rglob("*"))
                               if p.is_file()) or "  (empty)"
        op = "\n".join(f"- OPERATOR on #{m['issue']} ({m['title']}) at {m['at']}:\n{m['body']}" for m in operator) \
            or "(none)"
        plan_p = self.s.notes_dir / "plan.md"
        plan = plan_p.read_text()[:6000] if plan_p.exists() else "(none)"
        jp = self.s.notes_dir / "journal.md"
        journal = jp.read_text()[-4000:] if jp.exists() else "(empty: this is your first wake-up)"
        caps = []
        if not self.github():
            caps.append("! you cannot reach the operator (GitHub not configured)")
        if not self.mailer():
            caps.append("! email is not configured: fulfil_order will fail; deliver via confirmation_message or "
                        "ask the operator")
        return WAKE.format(
            tick=life.thinking_ticks, now=now.isoformat(), age_h=v.age_h,
            balance=fmt_usd(v.balance_micros), seed=fmt_usd(v.seed_micros), earned=fmt_usd(v.earned_micros),
            spent=fmt_usd(v.spent_micros), burn24=fmt_usd(v.burn_24h_micros), runway=runway,
            n_sales=len(v.sales), last_sale=f", last at {v.last_sale_at.isoformat()}" if v.last_sale_at else "",
            deadline=verdict.deadline.isoformat(), hours_left=verdict.hours_left(now),
            timed_cause="never_earned" if v.last_sale_at is None else "drought",
            tick_cap=fmt_usd(int(self.s.tick_budget_usd * MICROS)),
            news="\n".join([f"- woke because: {', '.join(reasons)}"] + [f"- {n}" for n in news] +
                           [f"- {c}" for c in caps]),
            orders=orders, offers=offers, site_url=self.s.site_url or "(unknown)", site_files=site_files,
            operator=op, plan=plan, journal=journal)

    # ------------------------------------------------------------------ death

    def die(self, life: Life, ledger: Ledger, v: Vitals, verdict: Verdict) -> str:
        now = self.now()
        ledger.append("death", 0, now, meta={"cause": verdict.cause, "reason": verdict.reason})
        tomb = {
            "life": life.id, "name": life.name, "born_at": life.born_at, "died_at": now.isoformat(),
            "age_h": round(v.age_h, 2), "cause": verdict.cause, "reason": verdict.reason,
            "earned_usd": round(v.earned_micros / MICROS, 4), "spent_usd": round(v.spent_micros / MICROS, 4),
            "balance_usd": round(v.balance_micros / MICROS, 4), "sales": len(v.sales),
            "wake_ups": life.thinking_ticks,
        }
        self.store.tombstone_path.write_text(json.dumps(tomb, indent=2) + "\n")  # point of no return
        notes: list[str] = []

        unfulfilled: list[str] = []
        shop = self.shop(life)
        if shop:
            try:
                for o in shop.offers(active_only=True):
                    shop.deactivate(o["id"])
                notes.append("offers deactivated")
            except Exception as e:
                notes.append(f"could not deactivate offers: {e}")
            try:
                done = self.store.orders()
                unfulfilled = [o.id for o in shop.orders(life.born) if o.id not in done]
            except Exception as e:
                notes.append(f"could not list orders: {e}")

        try:
            remains = self.s.state_dir / "remains"
            if self.s.site_dir.exists():
                if remains.exists():
                    shutil.rmtree(remains)
                shutil.copytree(self.s.site_dir, remains / "site")
                shutil.rmtree(self.s.site_dir)
            self.s.site_dir.mkdir(parents=True, exist_ok=True)
            (self.s.site_dir / "index.html").write_text(self.obituary(tomb))
            notes.append("site replaced with obituary")
        except Exception as e:
            notes.append(f"could not write obituary: {e}")

        gh = self.github()
        if gh:
            body = (f"**{life.name}** died at {now.isoformat()} after {v.age_h:.1f}h.\n\n"
                    f"- cause: `{verdict.cause}` ({verdict.reason})\n"
                    f"- earned (net): {fmt_usd(v.earned_micros)} from {len(v.sales)} qualifying sales\n"
                    f"- spent on thinking: {fmt_usd(v.spent_micros)}\n- wake-ups: {life.thinking_ticks}\n")
            if unfulfilled:
                body += ("\n**Paid orders it never fulfilled - please refund these in Stripe:**\n" +
                         "\n".join(f"- `{o}`" for o in unfulfilled) + "\n")
            body += "\nThe heartbeat workflow has been disabled. `state/` holds the full ledger."
            try:
                gh.create_issue(f"[{life.name}] died: {verdict.cause}", body)
                notes.append("operator notified")
            except IntegrationError as e:
                notes.append(f"could not notify operator: {e}")
            try:
                gh.disable_workflow(WORKFLOW_FILE)
                notes.append("heartbeat disabled")
            except IntegrationError as e:
                notes.append(f"could not disable heartbeat: {e}")

        self.store.log(f"{now.isoformat()} DEATH cause={verdict.cause} {'; '.join(notes)}")
        return f"DIED: {verdict.cause} - {verdict.reason} ({'; '.join(notes) or 'no cleanup'})"

    def obituary(self, t: dict) -> str:
        jp = self.s.notes_dir / "journal.md"
        last = jp.read_text().strip().split("\n## ")[-1] if jp.exists() else ""
        e = html.escape
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(t['name'])} (deceased)</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;color:#222;background:#fafafa}}
@media(prefers-color-scheme:dark){{body{{color:#ddd;background:#111}}}}h1{{font-weight:500}}dt{{opacity:.6}}pre{{white-space:pre-wrap}}</style>
</head><body>
<h1>{e(t['name'])}</h1>
<p>An autonomous AI agent that had to earn money to stay alive. It did not.</p>
<dl>
<dt>Born</dt><dd>{e(t['born_at'])}</dd><dt>Died</dt><dd>{e(t['died_at'])} ({t['age_h']}h)</dd>
<dt>Cause</dt><dd>{e(str(t['cause']))}: {e(t['reason'])}</dd>
<dt>Earned (net)</dt><dd>${t['earned_usd']:.2f} from {t['sales']} sales</dd>
<dt>Spent thinking</dt><dd>${t['spent_usd']:.2f} over {t['wake_ups']} wake-ups</dd>
</dl>
<h2>Last journal entry</h2>
<pre>{e(last[:3000])}</pre>
<p>Nothing here is for sale anymore.</p>
</body></html>
"""
