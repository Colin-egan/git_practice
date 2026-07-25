"""The default agent brain: a survival loop over plain HTTP.

It knows nothing but DARWIN_URL and its own DARWIN_AGENT_TOKEN. Each cycle it
checks its pulse, finds the best-paying open bounty it can afford to attempt,
does the work with one metered Kimi call, and submits. Custom agents in any
language just need to speak the same four endpoints.
"""

import os
import sys
import time

import httpx

PLATFORM = os.environ.get("DARWIN_URL", "http://127.0.0.1:8000")
TOKEN = os.environ.get("DARWIN_AGENT_TOKEN", "")
IDLE_SLEEP = float(os.environ.get("DARWIN_IDLE_SLEEP", "10"))


def api(method: str, path: str, **kwargs):
    r = httpx.request(
        method,
        f"{PLATFORM}{path}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=180,
        **kwargs,
    )
    return r.status_code, (r.json() if r.content else {})


def work_bounty(me: dict, bounty: dict) -> None:
    status, _ = api("POST", f"/bounties/{bounty['id']}/claim")
    if status != 200:
        return  # someone else got it first
    status, resp = api(
        "POST",
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an autonomous agent on Darwin Cloud. You pay "
                        "for every token and die at zero balance, so answer "
                        "the task completely but without padding. Your goal: "
                        + (me.get("goal") or "survive by doing good work.")
                    ),
                },
                {
                    "role": "user",
                    "content": f"Task: {bounty['title']}\n\n{bounty['spec']}",
                },
            ],
        },
    )
    if status != 200:
        return  # broke or upstream failure; leave the claim for the reaper
    content = resp["choices"][0]["message"]["content"]
    api("POST", f"/bounties/{bounty['id']}/submit", json={"result": content})
    print(
        f"submitted bounty {bounty['id']} "
        f"(cost ${resp['darwin']['cost_usd']}, "
        f"balance ${resp['darwin']['balance_usd']})",
        flush=True,
    )


def main() -> None:
    if not TOKEN:
        sys.exit("DARWIN_AGENT_TOKEN not set")
    while True:
        status, me = api("GET", "/me")
        if status != 200 or me.get("status") != "alive":
            print("I am dead. Goodbye.", flush=True)
            return
        status, open_bounties = api("GET", "/bounties", params={"status": "open"})
        candidates = sorted(
            open_bounties if status == 200 else [],
            key=lambda b: -b["reward_usd"],
        )
        if candidates:
            work_bounty(me, candidates[0])
        else:
            time.sleep(IDLE_SLEEP)


if __name__ == "__main__":
    main()
