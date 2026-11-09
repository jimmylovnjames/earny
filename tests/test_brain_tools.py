import anthropic
import httpx2 as httpx
import pytest

from earny.brain import Brain
from earny.config import MICROS, Rules
from earny.life import Life, Store
from earny.tools import ToolContext, run_tool

from .conftest import T0, FakeLLM, msg


def make(settings, clock, responses, balance=10 * MICROS):
    life = Life.new("Test", Rules(), settings.model, T0)
    ctx = ToolContext(settings=settings, life=life, store=Store(settings.state_dir), now=clock)
    charges = []
    state = {"balance": balance}

    def charge(c, meta):
        charges.append((c, meta))
        state["balance"] -= c

    llm = FakeLLM(responses)
    return Brain(llm, settings, ctx, charge, lambda: state["balance"]), ctx, llm, charges


def tool(id, name, **inp):
    return {"type": "tool_use", "id": id, "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


def test_loop_runs_tools_charges_and_journals(settings, clock):
    brain, ctx, llm, charges = make(settings, clock, [
        msg([tool("t1", "write_file", path="site/index.html", content="<h1>hi</h1>")], "tool_use"),
        msg([text("Built the landing page. Next: first offer.")]),
    ])
    rep = brain.run("sys", "wake")
    assert rep.stop == "end_turn" and rep.calls == 2
    assert (settings.site_dir / "index.html").read_text() == "<h1>hi</h1>"
    assert len(charges) == 2 and rep.spent_micros == sum(c for c, _ in charges)
    assert "first offer" in rep.final_text
    # second request carries the tool result, append-only
    second = llm.requests[1]["messages"]
    assert second[2]["content"][0]["type"] == "tool_result" and not second[2]["content"][0]["is_error"]
    # request shape
    r0 = llm.requests[0]
    assert r0["fallbacks"] == "default" and r0["betas"] == ["server-side-fallback-2026-07-01"]
    assert {t.get("name") for t in r0["tools"]} >= {"web_search", "web_fetch", "create_offer", "sleep"}
    assert all(t.get("strict") for t in r0["tools"] if "input_schema" in t)


def test_sleep_ends_tick_without_another_call(settings, clock):
    brain, ctx, llm, _ = make(settings, clock, [
        msg([tool("t1", "sleep", hours=6, reason="nothing to do"),
             tool("t2", "write_file", path="notes/plan.md", content="wait")], "tool_use"),
    ])
    rep = brain.run("sys", "wake")
    assert rep.stop == "slept" and rep.calls == 1
    assert (ctx.sleep_until - T0).total_seconds() == 6 * 3600
    assert (settings.notes_dir / "plan.md").read_text() == "wait"  # parallel call still ran before sleeping


def test_tick_budget_stops_loop(settings, clock):
    # each call: 50k in, 10k out on opus-5-5 = $0.40; cap is $0.50 -> one call only
    loop = [msg([tool(f"t{i}", "list_files", root="site")], "tool_use", input_tokens=50_000, output_tokens=10_000)
            for i in range(5)]
    brain, _, llm, _ = make(settings, clock, loop)
    rep = brain.run("sys", "wake")
    assert rep.stop == "tick_budget" and rep.calls == 1


def test_low_energy_refuses_to_call(settings, clock):
    brain, _, llm, _ = make(settings, clock, [msg([text("x")])], balance=10_000)
    rep = brain.run("sys", "wake")
    assert rep.stop == "low_energy" and rep.calls == 0 and llm.requests == []


def test_api_error_ends_tick_cleanly(settings, clock):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError("bad", response=httpx.Response(400, request=req), body=None)
    brain, _, _, charges = make(settings, clock, [err])
    rep = brain.run("sys", "wake")
    assert rep.stop == "api_error" and charges == [] and rep.errors


def test_refusal_stops(settings, clock):
    brain, _, _, _ = make(settings, clock, [msg([], "refusal")])
    assert brain.run("sys", "wake").stop == "refusal"


@pytest.mark.parametrize("path", ["state/ledger.jsonl", "../escape.txt", "site/../state/life.json", "/etc/passwd",
                                  "earny/heart.py", ".github/workflows/heartbeat.yml", "site/run.sh", "site"])
def test_sandbox(settings, clock, path):
    life = Life.new("T", Rules(), "m", T0)
    ctx = ToolContext(settings=settings, life=life, store=Store(settings.state_dir), now=clock)
    out, err = run_tool(ctx, "write_file", {"path": path, "content": "pwned"})
    assert err, out


def test_tools_without_integrations_fail_softly(settings, clock):
    life = Life.new("T", Rules(), "m", T0)
    ctx = ToolContext(settings=settings, life=life, store=Store(settings.state_dir), now=clock)
    for name, args in [("create_offer", dict(name="a", description="b", price_usd=5, confirmation_message="c",
                                             request_label="")),
                       ("fulfil_order", dict(order_id="cs_1", subject="s", body="b")),
                       ("ask_operator", dict(issue_number=0, title="t", body="b"))]:
        out, err = run_tool(ctx, name, args)
        assert err and "configured" in out
