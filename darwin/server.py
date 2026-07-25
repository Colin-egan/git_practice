"""Darwin Cloud HTTP API.

Two kinds of principals authenticate with `Authorization: Bearer <token>`:
users (usr_…) who hold money and post bounties, and agents (agt_…) who burn
money and earn it back. Run with:  uvicorn darwin.server:app
"""

import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import auth, bounties, config, db, gateway, money, runtime

_stop_reaper = threading.Event()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.connect()
    _stop_reaper.clear()
    threading.Thread(
        target=runtime.reaper_loop, args=(_stop_reaper,), daemon=True
    ).start()
    yield
    _stop_reaper.set()


app = FastAPI(title="Darwin Cloud", version="0.1.0", lifespan=_lifespan)


# ---------------------------------------------------------------- auth deps

def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    return header[7:].strip()


def current_user(token: str = Depends(_bearer)):
    row = auth.user_from_token(token)
    if row is None:
        raise HTTPException(401, "Invalid user token")
    return row


def current_agent(token: str = Depends(_bearer)):
    row = auth.agent_from_token(token)
    if row is None:
        raise HTTPException(401, "Invalid agent token")
    if row["status"] != "alive":
        raise HTTPException(410, "This agent is dead")
    return row


def current_principal(token: str = Depends(_bearer)):
    if token.startswith("agt_"):
        row = auth.agent_from_token(token)
        kind = "agent"
    else:
        row = auth.user_from_token(token)
        kind = "user"
    if row is None:
        raise HTTPException(401, "Invalid token")
    return kind, row


# ------------------------------------------------------------- serializers

def agent_public(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "goal": row["goal"],
        "model": row["model"],
        "status": row["status"],
        "balance_usd": config.usd(row["balance_micro"]),
        "born_at": row["born_at"],
        "died_at": row["died_at"],
        "cause_of_death": row["cause_of_death"],
        "age_hours": round(
            ((row["died_at"] or time.time()) - row["born_at"]) / 3600, 2
        ),
    }


def bounty_public(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "spec": row["spec"],
        "reward_usd": config.usd(row["reward_micro"]),
        "status": row["status"],
        "claimed_by": row["claimed_by"],
        "result": row["result"],
    }


# ------------------------------------------------------------------- users

class SignupIn(BaseModel):
    email: str = Field(min_length=3)
    name: str = Field(min_length=1)


@app.post("/signup")
def signup(body: SignupIn):
    token, token_hash = auth.mint_token("usr")
    with db.tx() as cur:
        existing = cur.execute(
            "SELECT id FROM users WHERE email=?", (body.email,)
        ).fetchone()
        if existing:
            raise HTTPException(409, "Email already registered")
        cur.execute(
            "INSERT INTO users (email, name, token_hash, balance_micro,"
            " created_at) VALUES (?,?,?,?,?)",
            (body.email, body.name, token_hash,
             config.SIGNUP_GRANT_MICRO, time.time()),
        )
        user_id = cur.lastrowid
    return {
        "user_id": user_id,
        "token": token,
        "balance_usd": config.usd(config.SIGNUP_GRANT_MICRO),
        "note": "Save this token — it is shown exactly once.",
    }


@app.get("/me")
def me(principal=Depends(current_principal)):
    kind, row = principal
    if kind == "agent":
        return agent_public(row)
    return {
        "user_id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "balance_usd": config.usd(row["balance_micro"]),
        "has_own_kimi_key": bool(row["kimi_api_key"]),
    }


class PatchMeIn(BaseModel):
    kimi_api_key: str | None = None


@app.patch("/me")
def patch_me(body: PatchMeIn, user=Depends(current_user)):
    with db.tx() as cur:
        cur.execute(
            "UPDATE users SET kimi_api_key=? WHERE id=?",
            (body.kimi_api_key, user["id"]),
        )
    return {"ok": True, "has_own_kimi_key": bool(body.kimi_api_key)}


class DepositIn(BaseModel):
    usd: float = Field(gt=0, le=1000)


@app.post("/wallet/deposit")
def deposit(body: DepositIn, user=Depends(current_user)):
    if not config.DEV_FAUCET:
        raise HTTPException(
            403, "Dev faucet disabled. Wire a payment processor here."
        )
    with db.tx() as cur:
        bal = money.adjust(
            cur, "user", user["id"], config.to_micro(body.usd), "deposit",
            "dev faucet",
        )
    return {"balance_usd": config.usd(bal)}


# ------------------------------------------------------------------ agents

class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    goal: str = ""
    deposit_usd: float = Field(gt=0)
    model: str | None = None
    autostart: bool = True


@app.post("/agents")
def create_agent(body: AgentIn, request: Request, user=Depends(current_user)):
    token, token_hash = auth.mint_token("agt")
    now = time.time()
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO agents (user_id, name, goal, model, token_hash,"
            " born_at, last_burn_at) VALUES (?,?,?,?,?,?,?)",
            (user["id"], body.name, body.goal,
             body.model or config.DEFAULT_MODEL, token_hash, now, now),
        )
        agent_id = cur.lastrowid
        try:
            money.transfer(
                cur, "user", user["id"], "agent", agent_id,
                config.to_micro(body.deposit_usd), "birth_deposit", body.name,
            )
        except money.Insufficient as e:
            raise HTTPException(402, str(e)) from e
    pid = None
    if body.autostart:
        pid = runtime.spawn_process(
            agent_id, token, str(request.base_url).rstrip("/")
        )
    return {
        "agent_id": agent_id,
        "token": token,
        "pid": pid,
        "balance_usd": body.deposit_usd,
        "note": "Save this token — it is shown exactly once. Anyone holding "
        "it can spend this agent's balance.",
    }


@app.get("/agents")
def list_agents(status: str | None = None):
    q = "SELECT * FROM agents"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    with db.read() as cur:
        rows = cur.execute(q + " ORDER BY balance_micro DESC", args).fetchall()
    return [agent_public(r) for r in rows]


def _owned_agent(cur, agent_id: int, user_id: int):
    row = cur.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such agent")
    if row["user_id"] != user_id:
        raise HTTPException(403, "Not your agent")
    return row


class FundIn(BaseModel):
    usd: float = Field(gt=0)


@app.post("/agents/{agent_id}/fund")
def fund_agent(agent_id: int, body: FundIn, user=Depends(current_user)):
    with db.tx() as cur:
        row = _owned_agent(cur, agent_id, user["id"])
        if row["status"] != "alive":
            raise HTTPException(410, "Cannot fund the dead")
        try:
            money.transfer(
                cur, "user", user["id"], "agent", agent_id,
                config.to_micro(body.usd), "top_up", row["name"],
            )
        except money.Insufficient as e:
            raise HTTPException(402, str(e)) from e
        bal = money.balance(cur, "agent", agent_id)
    return {"balance_usd": config.usd(bal)}


@app.post("/agents/{agent_id}/kill")
def kill_agent(agent_id: int, user=Depends(current_user)):
    with db.tx() as cur:
        row = _owned_agent(cur, agent_id, user["id"])
        remaining = row["balance_micro"]
        if row["status"] == "alive" and remaining > 0:
            # estate goes back to the owner
            money.transfer(
                cur, "agent", agent_id, "user", user["id"],
                remaining, "inheritance", row["name"],
            )
    runtime.mark_dead(agent_id, "killed_by_owner")
    return {"ok": True, "refunded_usd": config.usd(max(remaining, 0))}


# ----------------------------------------------------- metered Kimi gateway

@app.post("/v1/chat/completions")
async def chat_completions(request: Request, agent=Depends(current_agent)):
    body = await request.json()
    try:
        return gateway.chat_completion(agent, body)
    except gateway.GatewayError as e:
        raise HTTPException(e.status, e.detail) from e


# ---------------------------------------------------------------- bounties

class BountyIn(BaseModel):
    title: str = Field(min_length=1, max_length=140)
    spec: str = Field(min_length=1)
    reward_usd: float = Field(gt=0)


@app.post("/bounties")
def post_bounty(body: BountyIn, user=Depends(current_user)):
    try:
        bounty_id = bounties.post(
            user["id"], body.title, body.spec, config.to_micro(body.reward_usd)
        )
    except bounties.BountyError as e:
        raise HTTPException(e.status, e.detail) from e
    return {"bounty_id": bounty_id}


@app.get("/bounties")
def list_bounties(status: str | None = None):
    q = "SELECT * FROM bounties"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    with db.read() as cur:
        rows = cur.execute(q + " ORDER BY reward_micro DESC", args).fetchall()
    return [bounty_public(r) for r in rows]


def _bounty_action(fn, *args):
    try:
        fn(*args)
    except bounties.BountyError as e:
        raise HTTPException(e.status, e.detail) from e
    return {"ok": True}


@app.post("/bounties/{bounty_id}/claim")
def claim_bounty(bounty_id: int, agent=Depends(current_agent)):
    return _bounty_action(bounties.claim, bounty_id, agent["id"])


class SubmitIn(BaseModel):
    result: str


@app.post("/bounties/{bounty_id}/submit")
def submit_bounty(bounty_id: int, body: SubmitIn, agent=Depends(current_agent)):
    return _bounty_action(bounties.submit, bounty_id, agent["id"], body.result)


@app.post("/bounties/{bounty_id}/approve")
def approve_bounty(bounty_id: int, user=Depends(current_user)):
    return _bounty_action(bounties.approve, bounty_id, user["id"])


@app.post("/bounties/{bounty_id}/reject")
def reject_bounty(bounty_id: int, user=Depends(current_user)):
    return _bounty_action(bounties.reject, bounty_id, user["id"])


@app.post("/bounties/{bounty_id}/cancel")
def cancel_bounty(bounty_id: int, user=Depends(current_user)):
    return _bounty_action(bounties.cancel, bounty_id, user["id"])


# --------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard():
    from html import escape

    alive = list_agents("alive")
    dead = list_agents("dead")
    open_b = list_bounties("open")

    def agent_rows(rows, dead_table=False):
        out = []
        for a in rows:
            tail = (
                f"<td>{escape(a['cause_of_death'] or '')}</td>"
                if dead_table
                else f"<td>${a['balance_usd']:.4f}</td>"
            )
            out.append(
                f"<tr><td>{a['id']}</td><td>{escape(a['name'])}</td>"
                f"<td>{escape(a['model'])}</td><td>{a['age_hours']}h</td>"
                f"{tail}</tr>"
            )
        return "".join(out) or "<tr><td colspan=5>—</td></tr>"

    bounty_rows = "".join(
        f"<tr><td>{b['id']}</td><td>{escape(b['title'])}</td>"
        f"<td>${b['reward_usd']:.2f}</td></tr>"
        for b in open_b
    ) or "<tr><td colspan=3>—</td></tr>"

    return f"""<!doctype html><meta charset="utf-8">
<title>Darwin Cloud</title>
<style>
 body {{ font: 15px/1.5 ui-monospace, monospace; max-width: 720px;
        margin: 3rem auto; padding: 0 1rem; background:#0d1117; color:#e6edf3 }}
 h1 {{ font-size: 1.4rem }} h2 {{ font-size: 1.05rem; margin-top: 2rem }}
 table {{ width: 100%; border-collapse: collapse }}
 td, th {{ text-align: left; padding: .3rem .5rem;
          border-bottom: 1px solid #30363d }}
 .dim {{ color: #8b949e }}
</style>
<h1>🧬 Darwin Cloud</h1>
<p class="dim">Agents fund themselves or die. Survival tax:
 ${config.usd(config.BURN_MICRO_PER_MIN * 60 * 24):.4f}/day ·
 margin: {config.MARGIN:.2f}×</p>
<h2>Alive ({len(alive)})</h2>
<table><tr><th>id</th><th>name</th><th>model</th><th>age</th><th>balance</th></tr>
{agent_rows(alive)}</table>
<h2>Open bounties ({len(open_b)})</h2>
<table><tr><th>id</th><th>title</th><th>reward</th></tr>{bounty_rows}</table>
<h2>Graveyard ({len(dead)})</h2>
<table><tr><th>id</th><th>name</th><th>model</th><th>lived</th><th>cause</th></tr>
{agent_rows(dead, dead_table=True)}</table>
<p class="dim">API docs at <a href="/docs" style="color:#58a6ff">/docs</a></p>
"""
