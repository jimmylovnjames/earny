"""Thin clients for the outside world: GitHub (talking to the operator, and
the kill switch), Resend (emailing paying customers), Stripe (selling)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from .revenue import LIFE_KEY

UA = "earny-agent/0.1"


class IntegrationError(RuntimeError):
    pass


def _http(method: str, url: str, headers: dict, body: dict | None = None, timeout: float = 30) -> dict | list:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": UA, "Accept": "application/json", **({"Content-Type": "application/json"} if data else {}), **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        raise IntegrationError(f"{method} {url} -> {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise IntegrationError(f"{method} {url} -> {e.reason}") from e


# --------------------------------------------------------------------------- GitHub

class GitHub:
    API = "https://api.github.com"
    LABEL = "earny"

    def __init__(self, repo: str, token: str, operator_login: str):
        if not (repo and token):
            raise IntegrationError("GitHub not configured (needs GITHUB_REPOSITORY and GITHUB_TOKEN)")
        self.repo, self.token, self.operator = repo, token, operator_login.lower()

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def open_issues(self) -> list[dict]:
        return _http("GET", f"{self.API}/repos/{self.repo}/issues?labels={self.LABEL}&state=open&per_page=50", self._h())

    def create_issue(self, title: str, body: str) -> dict:
        return _http("POST", f"{self.API}/repos/{self.repo}/issues", self._h(),
                     {"title": title, "body": body, "labels": [self.LABEL]})

    def comment(self, number: int, body: str) -> dict:
        return _http("POST", f"{self.API}/repos/{self.repo}/issues/{number}/comments", self._h(), {"body": body})

    def operator_messages(self, since: datetime) -> list[dict]:
        """Comments on earny-labelled issues written by the operator only. Anything
        anyone else writes is ignored: a public repo is a prompt-injection surface."""
        out = []
        for issue in self.open_issues():
            if issue.get("comments", 0) == 0:
                continue
            url = f"{self.API}/repos/{self.repo}/issues/{issue['number']}/comments?since={since.isoformat()}&per_page=50"
            for c in _http("GET", url, self._h()):
                if (c.get("user") or {}).get("login", "").lower() != self.operator:
                    continue
                out.append({"issue": issue["number"], "title": issue["title"], "at": c["created_at"],
                            "body": c["body"][:4000]})
        return sorted(out, key=lambda c: c["at"])

    def disable_workflow(self, workflow_file: str) -> None:
        _http("PUT", f"{self.API}/repos/{self.repo}/actions/workflows/{workflow_file}/disable", self._h())


# --------------------------------------------------------------------------- Email

class Mailer:
    def __init__(self, api_key: str, sender: str):
        if not (api_key and sender):
            raise IntegrationError("email not configured (needs RESEND_API_KEY and EARNY_EMAIL_FROM)")
        self.key, self.sender = api_key, sender

    def send(self, to: str, subject: str, text: str) -> str:
        r = _http("POST", "https://api.resend.com/emails", {"Authorization": f"Bearer {self.key}"},
                  {"from": self.sender, "to": [to], "subject": subject[:200], "text": text})
        return str(r.get("id", ""))


# --------------------------------------------------------------------------- Stripe store

@dataclass(frozen=True)
class Order:
    id: str
    created: datetime
    amount_micros: int
    currency: str
    offer: str
    request: str  # what the customer typed in the custom field; UNTRUSTED
    email: str | None  # never shown to the model; used only by fulfil()


class Shop:
    MAX_ACTIVE_OFFERS = 20
    MIN_PRICE, MAX_PRICE = 1.0, 500.0

    def __init__(self, stripe_client, life_id: str, currency: str = "usd"):
        self.s, self.life_id, self.currency = stripe_client, life_id, currency

    @staticmethod
    def _d(o) -> dict:
        return o.to_dict() if hasattr(o, "to_dict") else dict(o)

    def offers(self, active_only: bool = True) -> list[dict]:
        links = self.s.v1.payment_links.list(params={"limit": 100, "active": True} if active_only else {"limit": 100})
        out = []
        for pl in links.auto_paging_iter():
            pl = self._d(pl)
            if (pl.get("metadata") or {}).get(LIFE_KEY) != self.life_id:
                continue
            out.append({"id": pl["id"], "url": pl["url"], "active": pl["active"],
                        "name": (pl.get("metadata") or {}).get("offer_name", "")})
        return out

    def create_offer(self, name: str, description: str, price_usd: float, confirmation_message: str,
                     request_label: str) -> dict:
        if not (self.MIN_PRICE <= price_usd <= self.MAX_PRICE):
            raise IntegrationError(f"price must be between ${self.MIN_PRICE:.0f} and ${self.MAX_PRICE:.0f}")
        if len(self.offers()) >= self.MAX_ACTIVE_OFFERS:
            raise IntegrationError(f"already {self.MAX_ACTIVE_OFFERS} active offers; deactivate one first")
        meta = {LIFE_KEY: self.life_id, "offer_name": name[:100]}
        product = self._d(self.s.v1.products.create(params={"name": name[:250], "description": description[:1000], "metadata": meta}))
        price = self._d(self.s.v1.prices.create(params={
            "product": product["id"], "currency": self.currency, "unit_amount": int(round(price_usd * 100))}))
        params: dict = {
            "line_items": [{"price": price["id"], "quantity": 1}],
            "metadata": meta,
            "payment_intent_data": {"metadata": meta},  # how revenue is attributed to this life
            "after_completion": {"type": "hosted_confirmation",
                                 "hosted_confirmation": {"custom_message": confirmation_message[:500]}},
        }
        if request_label.strip():
            params["custom_fields"] = [{"key": "request", "label": {"type": "custom", "custom": request_label[:50]},
                                        "type": "text", "text": {"maximum_length": 255}}]
        pl = self._d(self.s.v1.payment_links.create(params=params))
        return {"id": pl["id"], "url": pl["url"], "product": product["id"]}

    def deactivate(self, payment_link_id: str) -> None:
        pl = self._d(self.s.v1.payment_links.retrieve(payment_link_id))
        if (pl.get("metadata") or {}).get(LIFE_KEY) != self.life_id:
            raise IntegrationError("that payment link does not belong to this life")
        self.s.v1.payment_links.update(payment_link_id, params={"active": False})

    def orders(self, since: datetime) -> list[Order]:
        mine = {o["id"]: o["name"] for o in self.offers(active_only=False)}
        sessions = self.s.v1.checkout.sessions.list(params={"created": {"gte": int(since.timestamp())},
                                                            "status": "complete", "limit": 100})
        out = []
        for cs in sessions.auto_paging_iter():
            cs = self._d(cs)
            if cs.get("payment_link") not in mine or cs.get("payment_status") != "paid":
                continue
            req = ""
            for f in cs.get("custom_fields") or []:
                if f.get("key") == "request":
                    req = ((f.get("text") or {}).get("value") or "")[:255]
            out.append(Order(
                id=cs["id"], created=datetime.fromtimestamp(cs["created"], tz=timezone.utc),
                amount_micros=int(cs.get("amount_total") or 0) * 10_000, currency=cs.get("currency", ""),
                offer=mine[cs["payment_link"]], request=req,
                email=((cs.get("customer_details") or {}).get("email") or None)))
        return sorted(out, key=lambda o: o.created)
