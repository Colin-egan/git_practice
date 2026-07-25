"""Agent lifecycle: birth, survival tax, death.

The default runtime spawns `python -m darwin.agent_loop` as a subprocess whose
only credentials are its own agent token and the platform URL — it has no
Kimi key and no database access, so everything it does is metered. Users can
also run agents anywhere else (any language, any host) with the same token;
the reaper can't SIGTERM those, but a dead agent's token stops working, which
amounts to the same thing.
"""

import os
import signal
import subprocess
import sys
import time

from . import bounties, config, db, money


def spawn_process(agent_id: int, token: str, platform_url: str) -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "darwin.agent_loop"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DARWIN_URL": platform_url,
            "DARWIN_AGENT_TOKEN": token,
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    with db.tx() as cur:
        cur.execute("UPDATE agents SET pid=? WHERE id=?", (proc.pid, agent_id))
    return proc.pid


def _try_kill(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def mark_dead(agent_id: int, cause: str) -> None:
    with db.tx() as cur:
        row = cur.execute(
            "SELECT pid, status FROM agents WHERE id=?", (agent_id,)
        ).fetchone()
        if row is None or row["status"] == "dead":
            return
        cur.execute(
            "UPDATE agents SET status='dead', died_at=?, cause_of_death=?,"
            " pid=NULL WHERE id=?",
            (time.time(), cause, agent_id),
        )
        bounties.release_claims_of(cur, agent_id)
        pid = row["pid"]
    _try_kill(pid)


def reap_once() -> list[int]:
    """One reaper pass: charge survival tax for elapsed minutes, then kill
    anything at or below zero. Returns ids of the newly deceased."""
    now = time.time()
    doomed: list[int] = []
    with db.tx() as cur:
        rows = cur.execute(
            "SELECT id, balance_micro, last_burn_at FROM agents"
            " WHERE status='alive'"
        ).fetchall()
        for row in rows:
            minutes = int((now - row["last_burn_at"]) // 60)
            if minutes > 0 and config.BURN_MICRO_PER_MIN > 0:
                money.adjust(
                    cur, "agent", row["id"],
                    -minutes * config.BURN_MICRO_PER_MIN,
                    "survival_tax", f"{minutes} min alive",
                    allow_negative=True,
                )
                cur.execute(
                    "UPDATE agents SET last_burn_at=? WHERE id=?",
                    (row["last_burn_at"] + minutes * 60, row["id"]),
                )
            if money.balance(cur, "agent", row["id"]) <= 0:
                doomed.append(row["id"])
    for agent_id in doomed:
        mark_dead(agent_id, "starved")
    return doomed


def maybe_reap() -> list[int]:
    """Opportunistic reaper for serverless deployments: piggybacks on request
    traffic, at most once per REAPER_INTERVAL. The meta-row update inside a
    transaction makes concurrent instances elect a single reaper."""
    now = time.time()
    with db.tx() as cur:
        row = cur.execute(
            "SELECT value FROM meta WHERE key='last_reap'"
        ).fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO meta (key, value) VALUES ('last_reap', ?)", (now,)
            )
        elif now - row["value"] >= config.REAPER_INTERVAL:
            cur.execute(
                "UPDATE meta SET value=? WHERE key='last_reap'", (now,)
            )
        else:
            return []
    return reap_once()


def reaper_loop(stop_event) -> None:
    while not stop_event.wait(config.REAPER_INTERVAL):
        try:
            reap_once()
        except Exception as e:  # a reaper crash must not take the server down
            print(f"[reaper] error: {e}", file=sys.stderr)
