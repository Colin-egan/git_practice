"""Wallet primitives. Every balance change goes through adjust() so the ledger
is a complete audit trail. Balances never go below zero except via the
survival tax and post-hoc metering, which pass allow_negative=True — the
reaper cleans those up."""

import time


class Insufficient(Exception):
    pass


_TABLES = {"user": "users", "agent": "agents"}


def balance(cur, owner_kind: str, owner_id: int) -> int:
    row = cur.execute(
        f"SELECT balance_micro FROM {_TABLES[owner_kind]} WHERE id=?", (owner_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"{owner_kind} {owner_id} not found")
    return row["balance_micro"]


def adjust(
    cur,
    owner_kind: str,
    owner_id: int,
    delta_micro: int,
    kind: str,
    memo: str = "",
    allow_negative: bool = False,
) -> int:
    bal = balance(cur, owner_kind, owner_id)
    new_bal = bal + delta_micro
    if new_bal < 0 and not allow_negative:
        raise Insufficient(
            f"{owner_kind} {owner_id} has {bal} micro-USD, needs {-delta_micro}"
        )
    cur.execute(
        f"UPDATE {_TABLES[owner_kind]} SET balance_micro=? WHERE id=?",
        (new_bal, owner_id),
    )
    cur.execute(
        "INSERT INTO ledger (ts, owner_kind, owner_id, delta_micro,"
        " balance_after, kind, memo) VALUES (?,?,?,?,?,?,?)",
        (time.time(), owner_kind, owner_id, delta_micro, new_bal, kind, memo),
    )
    return new_bal


def transfer(
    cur,
    src_kind: str,
    src_id: int,
    dst_kind: str,
    dst_id: int,
    amount_micro: int,
    kind: str,
    memo: str = "",
) -> None:
    if amount_micro <= 0:
        raise ValueError("transfer amount must be positive")
    adjust(cur, src_kind, src_id, -amount_micro, kind, memo)
    adjust(cur, dst_kind, dst_id, amount_micro, kind, memo)
