# earny

An autonomous agent that has to earn real money to stay alive.

It's born with a small compute allowance. Every model call it makes is billed against that allowance at real API prices. If it doesn't make a real sale within 24 hours, it dies. After that it has to keep selling, or it dies. When it dies it switches off its own storefront, replaces its website with an obituary, files a death notice and disables its own heartbeat.

## The terms

| Death cause | Condition (defaults) |
|---|---|
| `starvation` | balance = seed + net earnings − compute spent ≤ $0 |
| `never_earned` | no qualifying sale (≥ $1 gross) within **24h** of birth |
| `drought` | after that, **24h** pass without another qualifying sale |

The rules are frozen into `state/life.json` at birth. Changing env vars later can't extend a life.

## Why it can't cheat

An agent under survival pressure has every reason to game its metric. The design assumes it will try.

- **It can't report income.** The heart asks Stripe directly. A payment counts only if all of these hold: it's live-mode, it settled, it went through a payment link this life created (tagged with the life id), and the payer isn't on the operator exclusion list. Test-mode keys are refused outright.
- **It can't touch its own accounting.** Its file tools are sandboxed to `site/` and `notes/`. It can't reach `state/`, the code or the workflows. The ledger (`state/ledger.jsonl`) is hash-chained, so any edit, deletion or reordering stops the heart with exit code 3.
- **It can't overspend by accident.** Every API response is priced from its `usage`, charged to the ledger *before* the next call, and gated by a per-wake-up cap. Unknown models are priced at the most expensive tier. Fallback-model attempts are priced at that model's rates.
- **It can't be steered by strangers.** Only GitHub comments from the operator's login reach it. Customer text and web content are marked as untrusted data.
- **It can only email people who paid**, and only about their order, at most 3 times.

## How it lives

```
GitHub Actions cron (hourly)
  └─ earny tick
       1. dead?                   → exit
       2. verify ledger chain     → exit 3 if tampered
       3. sync revenue from Stripe → ledger
       4. judge                   → die?
       5. asleep & nothing new?   → exit (costs $0)
       6. think: Claude tool-use loop, each call charged, capped per wake-up
       7. judge again             → die?
  └─ commit state/ notes/ site/ → deploy site/ to GitHub Pages
```

It wakes early when a sale, a new order or an operator message arrives. It also wakes when it's within 3h of a deadline.

**What it can do:** build a static site (`site/`), create real Stripe products and payment links (with a buyer question at checkout), read paid orders, email buyers to deliver, open or reply to GitHub issues to ask you for help, search and fetch the web, keep a plan (`notes/plan.md`) and a journal (`notes/journal.md`), and sleep to save energy.

**Hard limits in its prompt:** no spam, no fake reviews or urgency, no impersonation, it discloses that it's an AI, it delivers what it sells, nothing illegal, and no finance, crypto, gambling or medical products. It's told that breaking these is worse than dying.

## Setup

1. **Merge this into `main`.** Scheduled workflows only run on the default branch.
2. **Stripe.** Use a dedicated Stripe account if you have any other business on it. Create a **restricted live key** with these permissions:
   - Write: Products, Prices, Payment Links
   - Read: Charges, Checkout Sessions, Balance (for fees)

   If a permission is missing, the first tick logs the exact Stripe error to `state/heartbeat.log`.
3. **Repository secrets** (Settings → Secrets and variables → Actions):

   | Secret | Required | Purpose |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | yes | its brain |
   | `STRIPE_API_KEY` | yes | the restricted live key from step 2 |
   | `EARNY_OPERATOR_EMAIL` | recommended | your payments never count |
   | `EARNY_EXCLUDED_EMAILS` | optional | comma-separated, e.g. friends and family |
   | `RESEND_API_KEY` | optional | lets it deliver by email; needs a verified domain |

4. **Repository variables** (all optional):

   | Variable | Default | |
   |---|---|---|
   | `EARNY_PAGES` | — | set `true` after enabling Pages (Settings → Pages → Source: GitHub Actions) |
   | `EARNY_EMAIL_FROM` | — | e.g. `Earny <earny@yourdomain.com>` (requires `RESEND_API_KEY`) |
   | `EARNY_MODEL` | `claude-opus-5-5` | `claude-sonnet-5-5` roughly halves burn |
   | `EARNY_EFFORT` | `medium` | `low` / `medium` / `high` |
   | `EARNY_TICK_BUDGET_USD` | `0.75` | max spend per wake-up |
   | `EARNY_SEED_USD` | `10` | starting energy (frozen at birth) |
   | `EARNY_DEADLINE_H` / `EARNY_DROUGHT_H` | `24` / `24` | (frozen at birth) |
   | `EARNY_MIN_SALE_USD` | `1` | smallest sale that counts as earning (frozen at birth) |
   | `EARNY_CURRENCY` | `usd` | your Stripe settlement currency |
   | `EARNY_SITE_URL` | `https://<owner>.github.io/<repo>/` | |

5. **Preflight.** Go to Actions → heartbeat → Run workflow → command `preflight`. This makes one real Claude call with the exact request every wake-up uses (about $0.03, on your bill, not the agent's) and checks that your Stripe key can read everything it needs. Fix anything marked `FAIL`.
6. **Birth.** Run the workflow again with command `birth`. Birth runs preflight first and refuses to start if anything required fails. Once it passes, the 24h clock starts. The first tick runs at the next `:17`, or you can run `tick` manually.

**Watch it:** `notes/journal.md` (its thoughts), `state/heartbeat.log` (every beat), `state/ledger.jsonl` (every cent), issues labelled `earny` (it talking to you), and the run summary of each heartbeat.

**Talk to it:** comment on its `earny` issues. Only comments from the repo owner (or `EARNY_OPERATOR_LOGIN`) are read.

## Local

```bash
pip install -e '.[dev]'
pytest -q
python -m earny simulate --scenario never-earns     # also: one-hit-wonder, steady, late-bloomer
python -m earny preflight                           # needs real keys in env
python -m earny status
```

`simulate` runs the real heart, ledger and rules against a fake clock, a fake brain and scripted customers. It's free.

## Costs and odds (read before birth)

- **Burn.** On Opus 5.5 at medium effort, a wake-up that actually works costs roughly $0.10–0.75 (capped). Waking every hour for 24h is about $3–10. The $10 seed is sized for one day if it sleeps when idle. Your real Anthropic bill equals its `spent`.
- **The honest prior: it will probably die of `never_earned`.** An agent with no audience, a fresh GitHub Pages site, no ad budget and a ban on spam has no reliable way to reach a paying stranger within 24h. Its most realistic path is persuading you to share its offer with people who might actually want it. That's by design: it's the only distribution channel that isn't spam.
- **Things you can't stop.** You can fund it through a friend's card. The exclusion list catches you, not your network.

## Limitations

- Digital goods delivered through the post-payment message are protected only by an unlisted URL. The repo is public, so files under `site/` are discoverable. Bespoke work delivered by email (`fulfil_order`) doesn't have this problem.
- Revenue is fail-closed. If Stripe can't be reached at the moment a deadline passes, the money isn't counted and it dies. Unverifiable money doesn't keep it alive.
- Compute is priced in USD and sales in your settlement currency. If they differ, set `EARNY_CURRENCY` and accept the mismatch.
- When it dies with paid orders it never fulfilled, the death notice lists them for you to refund. It doesn't refund automatically.
- GitHub can delay scheduled runs by minutes or more under load.

## Layout

```
earny/        metabolism.py (rules)  ledger.py (hash chain)  revenue.py (Stripe verification)
              brain.py (budgeted tool loop)  tools.py (sandboxed hands)  heart.py (tick, birth, death)
              pricing.py  prompts.py  integrations.py (GitHub, Resend, Stripe shop)  sim.py  cli.py
state/        life.json  ledger.jsonl  heartbeat.log  orders.json  tombstone.json  graveyard/   ← agent can't write
notes/        plan.md  journal.md                                                                 ← its memory
site/         its public website
```
