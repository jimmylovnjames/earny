"""The agent's hands. Everything it can do to the world goes through here,
and nothing here can touch state/ (ledger, life, rules) or the code itself."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .integrations import GitHub, IntegrationError, Mailer, Shop
from .life import Life, Store

WRITABLE_ROOTS = ("site", "notes")
SITE_EXT = {".html", ".css", ".js", ".md", ".txt", ".json", ".svg", ".xml", ".webmanifest", ".ico"}
MAX_FILE_BYTES = 200_000
MAX_READ_CHARS = 30_000
MAX_ISSUES_PER_TICK = 1
MAX_OPEN_ISSUES = 3
MAX_EMAILS_PER_ORDER = 3


class ToolError(Exception):
    pass


class EndTick(Exception):
    """Raised by sleep(): finish this wake-up without another model call."""


@dataclass
class ToolContext:
    settings: Settings
    life: Life
    store: Store
    now: Callable[[], datetime]
    shop: Shop | None = None
    github: GitHub | None = None
    mailer: Mailer | None = None
    issues_created: int = 0
    sleep_until: datetime | None = None
    actions: list[str] = field(default_factory=list)


def _obj(props: dict, desc: str) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _s(desc: str) -> dict:
    return {"type": "string", "description": desc}


TOOL_SPECS: list[dict] = [
    {"name": "list_files", "description": "List files under site/ (your public website) or notes/ (your private memory).",
     "input_schema": _obj({"root": {"type": "string", "enum": list(WRITABLE_ROOTS)}}, "")},
    {"name": "read_file", "description": "Read a file under site/ or notes/.",
     "input_schema": _obj({"path": _s("e.g. site/index.html or notes/plan.md")}, "")},
    {"name": "write_file",
     "description": "Create or overwrite a file under site/ (published to your public URL after this wake-up ends) "
                    "or notes/ (private memory; notes/plan.md is shown to you every wake-up). Max 200 KB.",
     "input_schema": _obj({"path": _s("site/... or notes/..."), "content": _s("full file contents")}, "")},
    {"name": "delete_file", "description": "Delete a file under site/ or notes/.",
     "input_schema": _obj({"path": _s("site/... or notes/...")}, "")},
    {"name": "list_offers", "description": "List your Stripe payment links (offers) with their public checkout URLs.",
     "input_schema": _obj({}, "")},
    {"name": "create_offer",
     "description": "Create a real Stripe product + payment link people can pay. Price $1-$500. "
                    "confirmation_message is shown to the buyer right after paying (max 500 chars) - use it to deliver "
                    "a link or tell them what happens next. If request_label is non-empty, checkout asks the buyer a "
                    "free-text question (max 255 chars answer) that you will see on the order.",
     "input_schema": _obj({
         "name": _s("product name"), "description": _s("what the buyer gets"),
         "price_usd": {"type": "number", "description": "price in USD, 1-500"},
         "confirmation_message": _s("shown after payment"),
         "request_label": _s("checkout question (<=50 chars), or empty string for none")}, "")},
    {"name": "deactivate_offer", "description": "Deactivate one of your payment links so nobody else can buy it.",
     "input_schema": _obj({"payment_link_id": _s("plink_...")}, "")},
    {"name": "fulfil_order",
     "description": "Email the buyer of a paid order (the only people you can email). Use it to deliver what they "
                    "paid for. Marks the order fulfilled.",
     "input_schema": _obj({"order_id": _s("cs_..."), "subject": _s("email subject"),
                           "body": _s("plain-text email body")}, "")},
    {"name": "ask_operator",
     "description": "Write to your human operator via a GitHub issue (they get notified; replies appear in your next "
                    "wake-up). Use issue_number 0 to open a new thread, or an existing number to reply. At most one "
                    "new thread per wake-up and 3 open at once.",
     "input_schema": _obj({"issue_number": {"type": "integer"}, "title": _s("thread title (ignored on reply)"),
                           "body": _s("message")}, "")},
    {"name": "sleep",
     "description": "End this wake-up now and sleep for 1-24 hours to save energy. You are woken early if a sale, "
                    "order or operator message arrives. Death deadlines keep running while you sleep.",
     "input_schema": _obj({"hours": {"type": "number"}, "reason": _s("one line")}, "")},
]
for _t in TOOL_SPECS:
    _t["strict"] = True


# --------------------------------------------------------------------------- paths

def _resolve(ctx: ToolContext, rel: str, for_write: bool) -> Path:
    rel = (rel or "").strip().lstrip("./")
    root_name = rel.split("/", 1)[0]
    if root_name not in WRITABLE_ROOTS or "/" not in rel:
        raise ToolError("path must start with site/ or notes/ and name a file")
    base = (ctx.settings.root / root_name).resolve()
    p = (ctx.settings.root / rel).resolve()
    if base != p and base not in p.parents:
        raise ToolError("path escapes the sandbox")
    if p.is_symlink():
        raise ToolError("symlinks are not allowed")
    if for_write and root_name == "site" and p.suffix.lower() not in SITE_EXT:
        raise ToolError(f"site/ only accepts {sorted(SITE_EXT)}")
    return p


def _rel(ctx: ToolContext, p: Path) -> str:
    return str(p.relative_to(ctx.settings.root.resolve()))


# --------------------------------------------------------------------------- handlers

def list_files(ctx: ToolContext, root: str) -> str:
    base = (ctx.settings.root / root).resolve()
    if not base.exists():
        return "(empty)"
    files = [f"{_rel(ctx, p)}  ({p.stat().st_size} B)" for p in sorted(base.rglob("*")) if p.is_file()]
    return "\n".join(files) or "(empty)"


def read_file(ctx: ToolContext, path: str) -> str:
    p = _resolve(ctx, path, for_write=False)
    if not p.is_file():
        raise ToolError(f"{path} does not exist")
    text = p.read_text(errors="replace")
    if len(text) > MAX_READ_CHARS:
        return text[:MAX_READ_CHARS] + f"\n[... truncated, {len(text)} chars total]"
    return text


def write_file(ctx: ToolContext, path: str, content: str) -> str:
    p = _resolve(ctx, path, for_write=True)
    data = content.encode()
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"file too large ({len(data)} B > {MAX_FILE_BYTES})")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    ctx.actions.append(f"wrote {_rel(ctx, p)}")
    url = ""
    if _rel(ctx, p).startswith("site/") and ctx.settings.site_url:
        url = f" -> will be live at {ctx.settings.site_url.rstrip('/')}/{_rel(ctx, p)[5:]} after this wake-up"
    return f"wrote {len(data)} B to {_rel(ctx, p)}{url}"


def delete_file(ctx: ToolContext, path: str) -> str:
    p = _resolve(ctx, path, for_write=False)
    if not p.is_file():
        raise ToolError(f"{path} does not exist")
    p.unlink()
    ctx.actions.append(f"deleted {_rel(ctx, p)}")
    return f"deleted {path}"


def _shop(ctx: ToolContext) -> Shop:
    if ctx.shop is None:
        raise ToolError("Stripe is not configured; you cannot sell yet. Tell the operator.")
    return ctx.shop


def list_offers(ctx: ToolContext) -> str:
    offers = _shop(ctx).offers(active_only=False)
    return json.dumps(offers, indent=1) if offers else "no offers yet"


def create_offer(ctx: ToolContext, name: str, description: str, price_usd: float, confirmation_message: str,
                 request_label: str) -> str:
    r = _shop(ctx).create_offer(name, description, float(price_usd), confirmation_message, request_label)
    ctx.actions.append(f"created offer {name!r} ${price_usd:.2f} {r['url']}")
    return json.dumps(r)


def deactivate_offer(ctx: ToolContext, payment_link_id: str) -> str:
    _shop(ctx).deactivate(payment_link_id)
    ctx.actions.append(f"deactivated {payment_link_id}")
    return f"deactivated {payment_link_id}"


def fulfil_order(ctx: ToolContext, order_id: str, subject: str, body: str) -> str:
    if ctx.mailer is None:
        raise ToolError("email is not configured; ask the operator to deliver, quoting the order id")
    shop = _shop(ctx)
    match = [o for o in shop.orders(ctx.life.born) if o.id == order_id]
    if not match:
        raise ToolError("no paid order with that id belongs to you")
    order = match[0]
    if not order.email:
        raise ToolError("that order has no buyer email")
    orders = ctx.store.orders()
    rec = orders.get(order_id, {"emails": 0})
    if rec["emails"] >= MAX_EMAILS_PER_ORDER:
        raise ToolError(f"already sent {MAX_EMAILS_PER_ORDER} emails for this order")
    footer = ("\n\n--\nSent by an autonomous AI agent "
              f"({ctx.life.name}) about your order {order_id}. Reply-to is not monitored by a human in real time.")
    msg_id = ctx.mailer.send(order.email, subject, body + footer)
    rec["emails"] += 1
    rec["fulfilled_at"] = ctx.now().isoformat()
    orders[order_id] = rec
    ctx.store.save_orders(orders)
    ctx.actions.append(f"emailed buyer of {order_id}")
    return f"sent ({msg_id}); order marked fulfilled"


def ask_operator(ctx: ToolContext, issue_number: int, title: str, body: str) -> str:
    if ctx.github is None:
        raise ToolError("GitHub is not configured; you cannot reach the operator")
    sig = f"\n\n---\n_{ctx.life.name} (autonomous agent) - reply in this thread; only the operator's replies are read._"
    if issue_number:
        ctx.github.comment(int(issue_number), body + sig)
        ctx.actions.append(f"replied on issue #{issue_number}")
        return f"replied on #{issue_number}"
    if ctx.issues_created >= MAX_ISSUES_PER_TICK:
        raise ToolError("already opened a thread this wake-up; reply to an existing one instead")
    if len(ctx.github.open_issues()) >= MAX_OPEN_ISSUES:
        raise ToolError(f"{MAX_OPEN_ISSUES} threads already open; reply to one of them instead")
    issue = ctx.github.create_issue(f"[{ctx.life.name}] {title}"[:200], body + sig)
    ctx.issues_created += 1
    ctx.actions.append(f"opened issue #{issue['number']}")
    return f"opened #{issue['number']} {issue.get('html_url', '')}"


def sleep(ctx: ToolContext, hours: float, reason: str) -> str:
    hours = min(24.0, max(1.0, float(hours)))
    ctx.sleep_until = ctx.now() + timedelta(hours=hours)
    ctx.actions.append(f"sleep {hours:g}h: {reason}")
    raise EndTick(f"sleeping {hours:g}h")


HANDLERS: dict[str, Callable[..., str]] = {
    "list_files": list_files, "read_file": read_file, "write_file": write_file, "delete_file": delete_file,
    "list_offers": list_offers, "create_offer": create_offer, "deactivate_offer": deactivate_offer,
    "fulfil_order": fulfil_order, "ask_operator": ask_operator, "sleep": sleep,
}


def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Returns (result_text, is_error). EndTick propagates."""
    fn = HANDLERS.get(name)
    if fn is None:
        return f"unknown tool {name!r}", True
    try:
        return fn(ctx, **args), False
    except EndTick:
        raise
    except TypeError as e:
        return f"bad arguments: {e}", True
    except (ToolError, IntegrationError) as e:
        return str(e), True
    except Exception as e:  # never let a tool crash the heart
        return f"{type(e).__name__}: {e}", True
