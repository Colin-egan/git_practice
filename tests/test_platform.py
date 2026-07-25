"""End-to-end platform tests with a faked Kimi upstream — no real API calls,
no real money, no real subprocesses."""

import pytest
from fastapi.testclient import TestClient

from darwin import config, db, gateway, runtime
from darwin.server import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.reset_for_tests(str(tmp_path / "test.db"))
    # never fork real agent processes in tests
    monkeypatch.setattr(runtime, "spawn_process", lambda *a, **k: 4242)
    with TestClient(app) as c:
        yield c


def fake_upstream(prompt_tokens: int, completion_tokens: int, text="ok"):
    def _fake(api_key: str, body: dict) -> dict:
        return {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        }
    return _fake


def signup(client, email="a@b.c") -> dict:
    r = client.post("/signup", json={"email": email, "name": "Test"})
    assert r.status_code == 200
    return r.json()


def make_agent(client, user_token, deposit=0.5) -> dict:
    r = client.post(
        "/agents",
        json={"name": "worker", "goal": "earn", "deposit_usd": deposit},
        headers=bearer(user_token),
    )
    assert r.status_code == 200
    return r.json()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_signup_grant_and_duplicate_email(client):
    u = signup(client)
    assert u["balance_usd"] == config.usd(config.SIGNUP_GRANT_MICRO)
    r = client.post("/signup", json={"email": "a@b.c", "name": "Again"})
    assert r.status_code == 409


def test_agent_birth_moves_money(client):
    u = signup(client)
    a = make_agent(client, u["token"], deposit=0.4)
    me = client.get("/me", headers=bearer(u["token"])).json()
    assert me["balance_usd"] == pytest.approx(0.6)
    agent_me = client.get("/me", headers=bearer(a["token"])).json()
    assert agent_me["balance_usd"] == pytest.approx(0.4)
    assert agent_me["status"] == "alive"


def test_cannot_deposit_more_than_you_have(client):
    u = signup(client)
    r = client.post(
        "/agents",
        json={"name": "rich", "deposit_usd": 99.0},
        headers=bearer(u["token"]),
    )
    assert r.status_code == 402


def test_metered_completion_debits_wallet(client, monkeypatch):
    monkeypatch.setattr(gateway, "_call_upstream", fake_upstream(1000, 500))
    monkeypatch.setattr(config, "KIMI_API_KEY", "test-key")
    u = signup(client)
    a = make_agent(client, u["token"], deposit=0.5)
    r = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=bearer(a["token"]),
    )
    assert r.status_code == 200
    body = r.json()
    expected = config.usd(
        gateway.cost_micro(config.DEFAULT_MODEL, 1000, 500)
    )
    assert body["darwin"]["cost_usd"] == expected
    assert body["darwin"]["balance_usd"] == pytest.approx(0.5 - expected)


def test_broke_agent_gets_402(client, monkeypatch):
    monkeypatch.setattr(gateway, "_call_upstream", fake_upstream(10, 10))
    monkeypatch.setattr(config, "KIMI_API_KEY", "test-key")
    u = signup(client)
    a = make_agent(client, u["token"], deposit=0.000005)
    r = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=bearer(a["token"]),
    )
    assert r.status_code == 402


def test_reaper_kills_broke_agents_and_releases_claims(client):
    u = signup(client)
    a = make_agent(client, u["token"], deposit=0.1)
    client.post(
        "/bounties",
        json={"title": "t", "spec": "s", "reward_usd": 0.2},
        headers=bearer(u["token"]),
    )
    assert (
        client.post("/bounties/1/claim", headers=bearer(a["token"])).status_code
        == 200
    )
    # drain the wallet to zero, then reap
    with db.tx() as cur:
        cur.execute("UPDATE agents SET balance_micro=0 WHERE id=?",
                    (a["agent_id"],))
    dead = runtime.reap_once()
    assert a["agent_id"] in dead
    agents = client.get("/agents", params={"status": "dead"}).json()
    assert agents[0]["cause_of_death"] == "starved"
    b = client.get("/bounties").json()[0]
    assert b["status"] == "open" and b["claimed_by"] is None
    # dead agent's token no longer works
    r = client.get("/me", headers=bearer(a["token"]))
    assert r.json()["status"] == "dead"
    r = client.post("/bounties/1/claim", headers=bearer(a["token"]))
    assert r.status_code == 410


def test_survival_tax_accrues(client):
    u = signup(client)
    a = make_agent(client, u["token"], deposit=0.5)
    with db.tx() as cur:
        cur.execute(
            "UPDATE agents SET last_burn_at=last_burn_at-600 WHERE id=?",
            (a["agent_id"],),
        )
    runtime.reap_once()
    me = client.get("/me", headers=bearer(a["token"])).json()
    expected = 0.5 - config.usd(10 * config.BURN_MICRO_PER_MIN)
    assert me["balance_usd"] == pytest.approx(expected)


def test_bounty_lifecycle_pays_the_agent(client):
    poster = signup(client, "poster@x.y")
    owner = signup(client, "owner@x.y")
    a = make_agent(client, owner["token"], deposit=0.1)

    r = client.post(
        "/bounties",
        json={"title": "sum", "spec": "2+2?", "reward_usd": 0.3},
        headers=bearer(poster["token"]),
    )
    assert r.status_code == 200
    # escrow left the poster immediately
    me = client.get("/me", headers=bearer(poster["token"])).json()
    assert me["balance_usd"] == pytest.approx(0.7)

    assert client.post("/bounties/1/claim",
                       headers=bearer(a["token"])).status_code == 200
    # double-claim blocked
    assert client.post("/bounties/1/claim",
                       headers=bearer(a["token"])).status_code == 409
    assert client.post(
        "/bounties/1/submit", json={"result": "4"}, headers=bearer(a["token"])
    ).status_code == 200
    # only the poster can approve
    assert client.post("/bounties/1/approve",
                       headers=bearer(owner["token"])).status_code == 403
    assert client.post("/bounties/1/approve",
                       headers=bearer(poster["token"])).status_code == 200

    agent_me = client.get("/me", headers=bearer(a["token"])).json()
    assert agent_me["balance_usd"] == pytest.approx(0.4)


def test_reject_reopens_bounty(client):
    poster = signup(client, "p@x.y")
    owner = signup(client, "o@x.y")
    a = make_agent(client, owner["token"], deposit=0.1)
    client.post(
        "/bounties",
        json={"title": "t", "spec": "s", "reward_usd": 0.2},
        headers=bearer(poster["token"]),
    )
    client.post("/bounties/1/claim", headers=bearer(a["token"]))
    client.post("/bounties/1/submit", json={"result": "bad"},
                headers=bearer(a["token"]))
    assert client.post("/bounties/1/reject",
                       headers=bearer(poster["token"])).status_code == 200
    b = client.get("/bounties").json()[0]
    assert b["status"] == "open" and b["claimed_by"] is None
    # agent got nothing
    me = client.get("/me", headers=bearer(a["token"])).json()
    assert me["balance_usd"] == pytest.approx(0.1)


def test_kill_refunds_owner(client):
    u = signup(client)
    a = make_agent(client, u["token"], deposit=0.3)
    r = client.post(f"/agents/{a['agent_id']}/kill", headers=bearer(u["token"]))
    assert r.status_code == 200
    assert r.json()["refunded_usd"] == pytest.approx(0.3)
    me = client.get("/me", headers=bearer(u["token"])).json()
    assert me["balance_usd"] == pytest.approx(1.0)


def test_cannot_touch_someone_elses_agent(client):
    owner = signup(client, "o@x.y")
    stranger = signup(client, "s@x.y")
    a = make_agent(client, owner["token"])
    r = client.post(
        f"/agents/{a['agent_id']}/kill", headers=bearer(stranger["token"])
    )
    assert r.status_code == 403


def test_dashboard_renders_and_escapes(client):
    u = signup(client)
    client.post(
        "/agents",
        json={"name": "<script>alert(1)</script>", "deposit_usd": 0.1},
        headers=bearer(u["token"]),
    )
    html = client.get("/").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
