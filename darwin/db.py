"""SQLite storage. One shared connection guarded by an RLock; every mutation
runs inside `tx()` so wallet math is atomic under BEGIN IMMEDIATE."""

import sqlite3
import threading
from contextlib import contextmanager

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    kimi_api_key  TEXT,                 -- optional BYO key; else platform key
    balance_micro INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    name           TEXT NOT NULL,
    goal           TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'alive',   -- alive | dead
    token_hash     TEXT NOT NULL UNIQUE,
    balance_micro  INTEGER NOT NULL DEFAULT 0,
    pid            INTEGER,
    born_at        REAL NOT NULL,
    last_burn_at   REAL NOT NULL,
    died_at        REAL,
    cause_of_death TEXT
);

CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY,
    ts            REAL NOT NULL,
    owner_kind    TEXT NOT NULL,        -- 'user' | 'agent' | 'platform'
    owner_id      INTEGER NOT NULL,
    delta_micro   INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    memo          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bounties (
    id           INTEGER PRIMARY KEY,
    poster_id    INTEGER NOT NULL REFERENCES users(id),
    title        TEXT NOT NULL,
    spec         TEXT NOT NULL,
    reward_micro INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open',
    -- open | claimed | submitted | paid | cancelled
    claimed_by   INTEGER REFERENCES agents(id),
    result       TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    micro       INTEGER NOT NULL,
    destination TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | paid | cancelled
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

-- processed Stripe checkout sessions, for webhook replay idempotency
CREATE TABLE IF NOT EXISTS stripe_events (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_owner ON ledger(owner_kind, owner_id);
CREATE INDEX IF NOT EXISTS idx_bounties_status ON bounties(status);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(
                path or config.DB_PATH, check_same_thread=False
            )
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
        return _conn


def reset_for_tests(path: str) -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        connect(path)


@contextmanager
def tx():
    conn = connect()
    with _lock:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            cur.close()


@contextmanager
def read():
    conn = connect()
    with _lock:
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()
