"""The metered Kimi gateway.

Agents never see a real Moonshot key. They POST an OpenAI-compatible chat
completion to the platform with their agent token; we forward it upstream,
read the token usage off the response, and debit their wallet at
upstream-price × platform margin. Broke agents get HTTP 402 and, shortly
after, the reaper."""

import httpx

from . import config, db, money


class GatewayError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def cost_micro(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    in_per_m, out_per_m = config.price_for(model)
    raw = (prompt_tokens * in_per_m + completion_tokens * out_per_m) // 1_000_000
    return max(1, int(raw * config.MARGIN))


def worst_case_micro(model: str, body: dict) -> int:
    """Pre-flight estimate so an agent can't fire a request it could never
    afford: rough prompt size + the request's max_tokens at output price."""
    in_per_m, out_per_m = config.price_for(model)
    prompt_chars = sum(
        len(str(m.get("content", ""))) for m in body.get("messages", [])
    )
    est_prompt_tokens = max(1, prompt_chars // 3)
    max_out = min(
        int(body.get("max_tokens") or config.MAX_COMPLETION_TOKENS),
        config.MAX_COMPLETION_TOKENS,
    )
    raw = (est_prompt_tokens * in_per_m + max_out * out_per_m) // 1_000_000
    return max(1, int(raw * config.MARGIN))


def resolve_upstream_key(cur, agent_row) -> str:
    row = cur.execute(
        "SELECT kimi_api_key FROM users WHERE id=?", (agent_row["user_id"],)
    ).fetchone()
    key = (row["kimi_api_key"] if row else None) or config.KIMI_API_KEY
    if not key:
        raise GatewayError(
            503,
            "No Kimi API key configured. Set KIMI_API_KEY on the platform or "
            "attach your own key via PATCH /me.",
        )
    return key


def chat_completion(agent_row, body: dict) -> dict:
    """Meter + proxy one chat completion for an agent. Returns upstream JSON
    with a `darwin` billing block attached."""
    agent_id = agent_row["id"]
    model = body.get("model") or agent_row["model"] or config.DEFAULT_MODEL
    body = {**body, "model": model, "stream": False}
    body["max_tokens"] = min(
        int(body.get("max_tokens") or config.MAX_COMPLETION_TOKENS),
        config.MAX_COMPLETION_TOKENS,
    )

    with db.read() as cur:
        if agent_row["status"] != "alive":
            raise GatewayError(410, "This agent is dead. The reaper sends regards.")
        bal = money.balance(cur, "agent", agent_id)
        reserve = worst_case_micro(model, body)
        if bal < reserve:
            raise GatewayError(
                402,
                f"Insufficient funds: balance {config.usd(bal)} USD, this call "
                f"could cost up to {config.usd(reserve)} USD. Earn or die.",
            )
        upstream_key = resolve_upstream_key(cur, agent_row)

    resp = _call_upstream(upstream_key, body)

    usage = resp.get("usage") or {}
    cost = cost_micro(
        model,
        int(usage.get("prompt_tokens", 0)),
        int(usage.get("completion_tokens", 0)),
    )
    with db.tx() as cur:
        new_bal = money.adjust(
            cur,
            "agent",
            agent_id,
            -cost,
            "inference",
            f"{model} {usage.get('prompt_tokens', 0)}in/"
            f"{usage.get('completion_tokens', 0)}out",
            allow_negative=True,  # actual usage can exceed the estimate; reaper handles it
        )
    resp["darwin"] = {
        "cost_usd": config.usd(cost),
        "balance_usd": config.usd(new_bal),
    }
    return resp


def _call_upstream(api_key: str, body: dict) -> dict:
    try:
        r = httpx.post(
            f"{config.KIMI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=120,
        )
    except httpx.HTTPError as e:
        raise GatewayError(502, f"Upstream Kimi API unreachable: {e}") from e
    if r.status_code != 200:
        raise GatewayError(502, f"Upstream Kimi API error {r.status_code}: {r.text[:500]}")
    return r.json()
