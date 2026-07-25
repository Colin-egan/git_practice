"""All knobs live in environment variables so a docker-compose deploy can tune
them without code changes. Money is always integer micro-USD (1_000_000 = $1)."""

import json
import os

# Serverless mode (auto-on under Vercel): no reaper thread (an opportunistic
# per-request reaper runs instead) and no local agent subprocesses — agents
# connect from anywhere using their token.
SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("DARWIN_SERVERLESS"))

# On serverless the code dir is read-only; /tmp SQLite keeps the app alive
# (ephemeral, per-instance) until DATABASE_URL points at real Postgres.
DB_PATH = os.environ.get(
    "DARWIN_DB", "/tmp/darwin.db" if SERVERLESS else "darwin.db"
)

# Postgres DSN(s) for production. Comma-separated candidates are tried in
# order (transaction pooler first is the right call for serverless). Empty =
# local SQLite.
DATABASE_URLS = [
    u.strip() for u in os.environ.get("DATABASE_URL", "").split(",") if u.strip()
]
PG_SCHEMA_NAME = os.environ.get("PG_SCHEMA_NAME", "darwin")

# Demo mode: the gateway returns canned completions with plausible token
# usage instead of calling Moonshot — the whole economy works publicly with
# no upstream key. Unset it (and set KIMI_API_KEY) to go real.
KIMI_MOCK = os.environ.get("KIMI_MOCK", "") == "1"

# Upstream Kimi / Moonshot (OpenAI-compatible). Use api.moonshot.cn for the
# mainland endpoint.
KIMI_BASE_URL = os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
KIMI_API_KEY = os.environ.get("KIMI_API_KEY", "")
DEFAULT_MODEL = os.environ.get("KIMI_MODEL", "kimi-k2-0711-preview")

# Price per 1M tokens in micro-USD: {"model": [input, output]}.
# Verify against https://platform.moonshot.ai/docs/pricing before deploying —
# these defaults are a conservative snapshot, and unknown models are billed at
# FALLBACK_PRICING so nobody rides free on a new model id.
_default_pricing = {
    "kimi-k2-0711-preview": [600_000, 2_500_000],
    "kimi-k2-turbo-preview": [1_150_000, 8_000_000],
    "moonshot-v1-8k": [200_000, 2_000_000],
}
PRICING = json.loads(os.environ.get("PRICING_JSON", "null")) or _default_pricing
FALLBACK_PRICING = [2_000_000, 8_000_000]

# Platform margin on upstream cost. 1.20 = 20% markup; this is how the cloud
# itself gets funded.
MARGIN = float(os.environ.get("PLATFORM_MARGIN", "1.20"))

# Survival tax: micro-USD debited per agent per minute alive.
# 200/min ≈ $0.288/day — idling is affordable but not free.
BURN_MICRO_PER_MIN = int(os.environ.get("BURN_MICRO_PER_MIN", "200"))

# Reaper cadence in seconds.
REAPER_INTERVAL = float(os.environ.get("REAPER_INTERVAL", "15"))

# Dev faucet: lets users mint credits with POST /wallet/deposit. Turn this OFF
# and wire a real payment processor before letting strangers in.
DEV_FAUCET = os.environ.get("DEV_FAUCET", "1") == "1"

# Credits granted to a new user on signup (micro-USD).
SIGNUP_GRANT_MICRO = int(os.environ.get("SIGNUP_GRANT_MICRO", "1_000_000"))

# Hard cap on a single completion request's max_tokens, so one call can't
# drain a wallet past what the pre-flight check reserved.
MAX_COMPLETION_TOKENS = int(os.environ.get("MAX_COMPLETION_TOKENS", "4096"))

# --- Real-money edges (Stripe). All inert until STRIPE_SECRET_KEY is set. ---
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_API_BASE = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com")

# Public URL of this deployment, used for Checkout redirect targets.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000")

# Smallest cash-out, in micro-USD (default $5) — keeps payout overhead sane.
WITHDRAW_MIN_MICRO = int(os.environ.get("WITHDRAW_MIN_MICRO", "5_000_000"))

# Bearer token for admin endpoints (payout queue). Unset = admin API disabled.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def price_for(model: str) -> tuple[int, int]:
    p = PRICING.get(model, FALLBACK_PRICING)
    return int(p[0]), int(p[1])


def usd(micro: int) -> float:
    return round(micro / 1_000_000, 6)


def to_micro(usd_amount: float) -> int:
    return int(round(usd_amount * 1_000_000))
