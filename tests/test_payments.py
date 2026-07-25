"""Tests for the real-money edges: Stripe deposits (webhook-driven, replay-
safe), the withdrawal queue, and agent harvesting. Stripe itself is faked —
signatures are real HMACs computed with a test secret."""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from darwin import config, db, payments, runtime
from darwin.server import app

WEBHOOK_SECRET = "whsec_testsecret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.reset_for_tests(str(tmp_path / "test.db"))
    monkeypatch.setattr(runtime, "spawn_process", lambda *a, **k: 4242)
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "admin-secret")
    with TestClient(app) as c:
        yield c


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def signup(client, email="a@b.c") -> dict:
    return client.post("/signup", json={"email": email, "name": "T"}).json()


def signed_webhook(session_id: str, user_id: int, cents: int) -> tuple[bytes, str]:
    payload = json.dumps(
        {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "amount_total": cents,
                    "metadata": {"user_id": str(user_id)},
                }
            },
        }
    ).encode()
    ts = int(time.time())
    sig = hmac.new(
        WEBHOOK_SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return payload, f"t={ts},v1={sig}"


def test_checkout_calls_stripe(client, monkeypatch):
    captured = {}

    def fake_post(url, auth=None, data=None, timeout=None):
        captured.update(url=url, auth=auth, data=data)

        class R:
            status_code = 200

            def json(self):
                return {"url": "https://checkout.stripe.com/pay/cs_test"}

        return R()

    monkeypatch.setattr(payments.httpx, "post", fake_post)
    u = signup(client)
    r = client.post(
        "/wallet/checkout", json={"usd": 10}, headers=bearer(u["token"])
    )
    assert r.status_code == 200
    assert r.json()["checkout_url"].startswith("https://checkout.stripe.com")
    assert captured["data"]["line_items[0][price_data][unit_amount]"] == "1000"
    assert captured["data"]["metadata[user_id]"] == str(u["user_id"])


def test_checkout_minimum(client):
    u = signup(client)
    r = client.post(
        "/wallet/checkout", json={"usd": 0.10}, headers=bearer(u["token"])
    )
    assert r.status_code == 400


def test_webhook_credits_wallet_once(client):
    u = signup(client)
    payload, sig = signed_webhook("cs_1", u["user_id"], 1000)  # $10
    r = client.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": sig}
    )
    assert r.status_code == 200
    assert r.json()["credited_usd"] == 10.0
    me = client.get("/me", headers=bearer(u["token"])).json()
    assert me["balance_usd"] == pytest.approx(11.0)  # $1 grant + $10
    # replayed event must not double-credit
    r = client.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": sig}
    )
    assert r.json().get("duplicate") is True
    me = client.get("/me", headers=bearer(u["token"])).json()
    assert me["balance_usd"] == pytest.approx(11.0)


def test_webhook_rejects_bad_signature(client):
    u = signup(client)
    payload, _ = signed_webhook("cs_2", u["user_id"], 1000)
    r = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": f"t={int(time.time())},v1=deadbeef"},
    )
    assert r.status_code == 400
    me = client.get("/me", headers=bearer(u["token"])).json()
    assert me["balance_usd"] == pytest.approx(1.0)


def test_webhook_ignores_other_events(client):
    payload = json.dumps({"type": "invoice.paid", "data": {"object": {}}}).encode()
    ts = int(time.time())
    sig = hmac.new(
        WEBHOOK_SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    r = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": f"t={ts},v1={sig}"},
    )
    assert r.status_code == 200 and r.json()["ignored"] == "invoice.paid"


def test_withdrawal_holds_funds_and_admin_pays(client):
    u = signup(client)
    payload, sig = signed_webhook("cs_3", u["user_id"], 2000)  # $20
    client.post("/webhooks/stripe", content=payload,
                headers={"stripe-signature": sig})

    r = client.post(
        "/wallet/withdraw",
        json={"usd": 8, "destination": "paypal:me@x.y"},
        headers=bearer(u["token"]),
    )
    assert r.status_code == 200
    wid = r.json()["withdrawal_id"]
    # funds held immediately
    me = client.get("/me", headers=bearer(u["token"])).json()
    assert me["balance_usd"] == pytest.approx(13.0)

    # admin sees it, pays it
    q = client.get("/admin/withdrawals", headers=bearer("admin-secret")).json()
    assert [w["id"] for w in q] == [wid]
    assert client.post(
        f"/admin/withdrawals/{wid}/paid", headers=bearer("admin-secret")
    ).status_code == 200
    # can't pay twice
    assert client.post(
        f"/admin/withdrawals/{wid}/paid", headers=bearer("admin-secret")
    ).status_code == 409
    mine = client.get("/wallet/withdrawals", headers=bearer(u["token"])).json()
    assert mine[0]["status"] == "paid"


def test_withdrawal_cancel_refunds(client):
    u = signup(client)
    payload, sig = signed_webhook("cs_4", u["user_id"], 2000)
    client.post("/webhooks/stripe", content=payload,
                headers={"stripe-signature": sig})
    wid = client.post(
        "/wallet/withdraw",
        json={"usd": 6, "destination": "paypal:me@x.y"},
        headers=bearer(u["token"]),
    ).json()["withdrawal_id"]
    client.post(f"/admin/withdrawals/{wid}/cancel", headers=bearer("admin-secret"))
    me = client.get("/me", headers=bearer(u["token"])).json()
    assert me["balance_usd"] == pytest.approx(21.0)  # fully refunded


def test_withdrawal_limits(client):
    u = signup(client)
    # below minimum
    r = client.post(
        "/wallet/withdraw",
        json={"usd": 1, "destination": "d"},
        headers=bearer(u["token"]),
    )
    assert r.status_code == 400
    # more than balance
    r = client.post(
        "/wallet/withdraw",
        json={"usd": 50, "destination": "d"},
        headers=bearer(u["token"]),
    )
    assert r.status_code == 402


def test_admin_requires_token(client):
    r = client.get("/admin/withdrawals", headers=bearer("wrong"))
    assert r.status_code == 403


def test_harvest_moves_agent_earnings_to_owner(client):
    u = signup(client)
    a = client.post(
        "/agents",
        json={"name": "earner", "deposit_usd": 0.9},
        headers=bearer(u["token"]),
    ).json()
    r = client.post(
        f"/agents/{a['agent_id']}/harvest",
        json={"usd": 0.4},
        headers=bearer(u["token"]),
    )
    assert r.status_code == 200
    assert r.json()["agent_balance_usd"] == pytest.approx(0.5)
    me = client.get("/me", headers=bearer(u["token"])).json()
    assert me["balance_usd"] == pytest.approx(0.5)
    # can't harvest more than the agent has
    r = client.post(
        f"/agents/{a['agent_id']}/harvest",
        json={"usd": 5},
        headers=bearer(u["token"]),
    )
    assert r.status_code == 402
