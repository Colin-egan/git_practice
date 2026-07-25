"""The bounty board — how agents earn.

A poster escrows the reward from their user wallet when the bounty opens, so
payouts can never bounce. An agent claims, works, submits; the poster approves
(escrow → agent wallet) or rejects (bounty reopens). Cancelling an open bounty
refunds the escrow."""

import time

from . import db, money


class BountyError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def _get(cur, bounty_id: int):
    row = cur.execute("SELECT * FROM bounties WHERE id=?", (bounty_id,)).fetchone()
    if row is None:
        raise BountyError(404, "No such bounty")
    return row


def post(poster_id: int, title: str, spec: str, reward_micro: int) -> int:
    if reward_micro <= 0:
        raise BountyError(400, "Reward must be positive")
    now = time.time()
    with db.tx() as cur:
        try:
            money.adjust(
                cur, "user", poster_id, -reward_micro, "bounty_escrow", title
            )
        except money.Insufficient as e:
            raise BountyError(402, str(e)) from e
        cur.execute(
            "INSERT INTO bounties (poster_id, title, spec, reward_micro,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (poster_id, title, spec, reward_micro, "open", now, now),
        )
        return cur.lastrowid


def claim(bounty_id: int, agent_id: int) -> None:
    with db.tx() as cur:
        b = _get(cur, bounty_id)
        if b["status"] != "open":
            raise BountyError(409, f"Bounty is {b['status']}, not open")
        cur.execute(
            "UPDATE bounties SET status='claimed', claimed_by=?, updated_at=?"
            " WHERE id=?",
            (agent_id, time.time(), bounty_id),
        )


def submit(bounty_id: int, agent_id: int, result: str) -> None:
    with db.tx() as cur:
        b = _get(cur, bounty_id)
        if b["claimed_by"] != agent_id:
            raise BountyError(403, "You did not claim this bounty")
        if b["status"] != "claimed":
            raise BountyError(409, f"Bounty is {b['status']}, not claimed")
        cur.execute(
            "UPDATE bounties SET status='submitted', result=?, updated_at=?"
            " WHERE id=?",
            (result, time.time(), bounty_id),
        )


def approve(bounty_id: int, poster_id: int) -> None:
    with db.tx() as cur:
        b = _get(cur, bounty_id)
        if b["poster_id"] != poster_id:
            raise BountyError(403, "Only the poster can approve")
        if b["status"] != "submitted":
            raise BountyError(409, f"Bounty is {b['status']}, not submitted")
        money.adjust(
            cur, "agent", b["claimed_by"], b["reward_micro"],
            "bounty_payout", b["title"],
        )
        cur.execute(
            "UPDATE bounties SET status='paid', updated_at=? WHERE id=?",
            (time.time(), bounty_id),
        )


def reject(bounty_id: int, poster_id: int) -> None:
    """Send it back to the pool; the agent ate the inference cost. Harsh, but
    that's the business model."""
    with db.tx() as cur:
        b = _get(cur, bounty_id)
        if b["poster_id"] != poster_id:
            raise BountyError(403, "Only the poster can reject")
        if b["status"] != "submitted":
            raise BountyError(409, f"Bounty is {b['status']}, not submitted")
        cur.execute(
            "UPDATE bounties SET status='open', claimed_by=NULL, result=NULL,"
            " updated_at=? WHERE id=?",
            (time.time(), bounty_id),
        )


def cancel(bounty_id: int, poster_id: int) -> None:
    with db.tx() as cur:
        b = _get(cur, bounty_id)
        if b["poster_id"] != poster_id:
            raise BountyError(403, "Only the poster can cancel")
        if b["status"] != "open":
            raise BountyError(409, f"Bounty is {b['status']}, not open")
        money.adjust(
            cur, "user", poster_id, b["reward_micro"],
            "bounty_refund", b["title"],
        )
        cur.execute(
            "UPDATE bounties SET status='cancelled', updated_at=? WHERE id=?",
            (time.time(), bounty_id),
        )


def release_claims_of(cur, agent_id: int) -> None:
    """When an agent dies mid-job, its claimed bounties go back to the pool."""
    cur.execute(
        "UPDATE bounties SET status='open', claimed_by=NULL, updated_at=?"
        " WHERE claimed_by=? AND status IN ('claimed','submitted')",
        (time.time(), agent_id),
    )
