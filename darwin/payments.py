"""The real-money edges of the credit economy.

Inside Darwin Cloud everything is credits; real dollars only cross the
boundary in two places, both here:

  in:  Stripe Checkout -> signature-verified webhook -> user wallet credits
  out: withdrawal queue -> credits held immediately -> admin pays out and
       marks it paid (wire Stripe Connect transfers here to automate)

Stripe is spoken over plain REST with httpx — no SDK dependency. Everything
is inert unless STRIPE_SECRET_KEY is set.
"""

import hashlib
import hmac
import json
import time

import httpx

from . import config, db, money


class PaymentError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def enabled() -> bool:
    return bool(config.STRIPE_SECRET_KEY)


# ------------------------------------------------------------------ deposits

def create_checkout(user_id: int, micro: int) -> str:
    """Create a Stripe Checkout session for a credit top-up. Returns the URL
    the user completes payment at; the webhook does the actual crediting."""
    if not enabled():
        raise PaymentError(503, "Stripe not configured (STRIPE_SECRET_KEY)")
    cents = micro // 10_000
    if cents < 50:
        raise PaymentError(400, "Minimum deposit is $0.50")
    base = config.PUBLIC_BASE_URL.rstrip("/")
    form = {
        "mode": "payment",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(cents),
        "line_items[0][price_data][product_data][name]": "Darwin Cloud credits",
        "success_url": f"{base}/?deposit=success",
        "cancel_url": f"{base}/?deposit=cancelled",
        "metadata[user_id]": str(user_id),
    }
    try:
        r = httpx.post(
            f"{config.STRIPE_API_BASE}/v1/checkout/sessions",
            auth=(config.STRIPE_SECRET_KEY, ""),
            data=form,
            timeout=30,
        )
    except httpx.HTTPError as e:
        raise PaymentError(502, f"Stripe unreachable: {e}") from e
    if r.status_code != 200:
        raise PaymentError(502, f"Stripe error {r.status_code}: {r.text[:300]}")
    return r.json()["url"]


def verify_signature(payload: bytes, sig_header: str, tolerance: int = 300) -> None:
    """Stripe-Signature: t=<ts>,v1=<hmac>[,v1=...] — HMAC-SHA256 of
    '<ts>.<payload>' with the webhook secret."""
    pairs = [p.split("=", 1) for p in sig_header.split(",") if "=" in p]
    ts = next((v for k, v in pairs if k == "t"), None)
    sigs = [v for k, v in pairs if k == "v1"]
    if ts is None or not sigs:
        raise PaymentError(400, "Malformed Stripe-Signature header")
    if abs(time.time() - int(ts)) > tolerance:
        raise PaymentError(400, "Webhook timestamp outside tolerance")
    expected = hmac.new(
        config.STRIPE_WEBHOOK_SECRET.encode(),
        f"{ts}.".encode() + payload,
        hashlib.sha256,
    ).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise PaymentError(400, "Invalid webhook signature")


def handle_webhook(payload: bytes, sig_header: str) -> dict:
    if not config.STRIPE_WEBHOOK_SECRET:
        raise PaymentError(503, "Stripe webhook secret not configured")
    verify_signature(payload, sig_header)
    event = json.loads(payload)
    if event.get("type") != "checkout.session.completed":
        return {"ok": True, "ignored": event.get("type")}
    obj = event["data"]["object"]
    session_id = obj["id"]
    user_id = int(obj["metadata"]["user_id"])
    micro = int(obj["amount_total"]) * 10_000  # cents -> micro-USD
    with db.tx() as cur:
        if cur.execute(
            "SELECT 1 FROM stripe_events WHERE id=?", (session_id,)
        ).fetchone():
            return {"ok": True, "duplicate": True}
        cur.execute(
            "INSERT INTO stripe_events (id, ts) VALUES (?,?)",
            (session_id, time.time()),
        )
        money.adjust(
            cur, "user", user_id, micro, "deposit", f"stripe {session_id}"
        )
    return {"ok": True, "credited_usd": config.usd(micro)}


# --------------------------------------------------------------- withdrawals

def request_withdrawal(user_id: int, micro: int, destination: str) -> int:
    """Debit credits now, queue a real-money payout. The hold means a payout
    can never exceed what the user actually had."""
    if micro < config.WITHDRAW_MIN_MICRO:
        raise PaymentError(
            400,
            f"Minimum withdrawal is {config.usd(config.WITHDRAW_MIN_MICRO)} USD",
        )
    if not destination.strip():
        raise PaymentError(400, "Destination required (e.g. PayPal email)")
    with db.tx() as cur:
        try:
            money.adjust(
                cur, "user", user_id, -micro, "withdrawal_hold", destination
            )
        except money.Insufficient as e:
            raise PaymentError(402, str(e)) from e
        cur.execute(
            "INSERT INTO withdrawals (user_id, micro, destination, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (user_id, micro, destination, "pending", time.time(), time.time()),
        )
        return cur.lastrowid


def _get(cur, withdrawal_id: int):
    row = cur.execute(
        "SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)
    ).fetchone()
    if row is None:
        raise PaymentError(404, "No such withdrawal")
    return row


def mark_paid(withdrawal_id: int) -> None:
    with db.tx() as cur:
        w = _get(cur, withdrawal_id)
        if w["status"] != "pending":
            raise PaymentError(409, f"Withdrawal is {w['status']}, not pending")
        cur.execute(
            "UPDATE withdrawals SET status='paid', updated_at=? WHERE id=?",
            (time.time(), withdrawal_id),
        )


def cancel(withdrawal_id: int) -> None:
    with db.tx() as cur:
        w = _get(cur, withdrawal_id)
        if w["status"] != "pending":
            raise PaymentError(409, f"Withdrawal is {w['status']}, not pending")
        money.adjust(
            cur, "user", w["user_id"], w["micro"], "withdrawal_refund",
            w["destination"],
        )
        cur.execute(
            "UPDATE withdrawals SET status='cancelled', updated_at=? WHERE id=?",
            (time.time(), withdrawal_id),
        )
