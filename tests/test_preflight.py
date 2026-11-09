from dataclasses import replace
from types import SimpleNamespace

from earny import preflight

from .conftest import FakeLLM, msg


class OkStripe:
    def __init__(self, fail=()):
        def svc(name):
            def lst(params=None):
                if name in fail:
                    raise PermissionError(f"missing {name} permission")
                return SimpleNamespace(data=[])
            return SimpleNamespace(list=lst)
        self.v1 = SimpleNamespace(charges=svc("charges"), payment_links=svc("payment_links"),
                                  products=svc("products"), checkout=SimpleNamespace(sessions=svc("sessions")))


def test_ready_when_required_checks_pass(settings):
    s = replace(settings, stripe_key="rk_live_x")
    llm = FakeLLM([msg([{"type": "text", "text": "OK"}], input_tokens=3000, output_tokens=20)])
    text, ok = preflight.report(preflight.run(s, llm=llm, stripe_client=OkStripe()))
    assert ok, text
    req = llm.requests[0]
    assert req["fallbacks"] == "default" and any(t.get("name") == "web_search" for t in req["tools"])


def test_not_ready_on_missing_stripe_permission(settings):
    s = replace(settings, stripe_key="rk_live_x")
    llm = FakeLLM([msg([{"type": "text", "text": "OK"}])])
    text, ok = preflight.report(preflight.run(s, llm=llm, stripe_client=OkStripe(fail={"sessions"})))
    assert not ok and "[FAIL] stripe: read checkout sessions" in text


def test_not_ready_on_test_key_or_api_error(settings):
    s = replace(settings, stripe_key="sk_test_x")
    llm = FakeLLM([RuntimeError("400 invalid request")])
    text, ok = preflight.report(preflight.run(s, llm=llm, stripe_client=OkStripe()))
    assert not ok and "TEST key" in text and "[FAIL] anthropic" in text
