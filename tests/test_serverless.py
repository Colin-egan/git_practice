"""Tests for serverless-mode behaviors: demo-mode gateway, opportunistic
reaper throttling, and suppressed subprocess autostart."""

import pytest
from fastapi.testclient import TestClient

from darwin import config, db, runtime
from darwin.server import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.reset_for_tests(str(tmp_path / "test.db"))
    monkeypatch.setattr(runtime, "spawn_process", lambda *a, **k: 4242)
    with TestClient(app) as c:
        yield c


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def signup(client, email="a@b.c") -> dict:
    return client.post("/signup", json={"email": email, "name": "T"}).json()


def test_mock_gateway_bills_without_upstream(client, monkeypatch):
    monkeypatch.setattr(config, "KIMI_MOCK", True)
    u = signup(client)
    a = client.post(
        "/agents",
        json={"name": "w", "deposit_usd": 0.5},
        headers=bearer(u["token"]),
    ).json()
    r = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello world"}]},
        headers=bearer(a["token"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert "demo mode" in body["choices"][0]["message"]["content"]
    assert body["darwin"]["cost_usd"] > 0
    assert body["darwin"]["balance_usd"] < 0.5


def test_maybe_reap_throttles(client, monkeypatch):
    monkeypatch.setattr(config, "REAPER_INTERVAL", 3600)
    u = signup(client)
    a = client.post(
        "/agents",
        json={"name": "w", "deposit_usd": 0.1},
        headers=bearer(u["token"]),
    ).json()
    runtime.maybe_reap()  # first call sets the marker
    with db.tx() as cur:
        cur.execute(
            "UPDATE agents SET balance_micro=0 WHERE id=?", (a["agent_id"],)
        )
    # inside the interval: throttled, agent survives
    assert runtime.maybe_reap() == []
    with db.tx() as cur:
        cur.execute("UPDATE meta SET value=0 WHERE key='last_reap'")
    # interval elapsed: reaped
    assert runtime.maybe_reap() == [a["agent_id"]]


def test_serverless_suppresses_autostart(client, monkeypatch):
    monkeypatch.setattr(config, "SERVERLESS", True)
    spawned = []
    monkeypatch.setattr(
        runtime, "spawn_process", lambda *a, **k: spawned.append(a) or 4242
    )
    u = signup(client)
    r = client.post(
        "/agents",
        json={"name": "w", "deposit_usd": 0.1, "autostart": True},
        headers=bearer(u["token"]),
    )
    assert r.status_code == 200
    assert r.json()["pid"] is None
    assert spawned == []
    assert "run your agent anywhere" in r.json()["note"]
