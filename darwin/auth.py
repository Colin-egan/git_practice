"""Bearer-token auth. Secrets are shown once at creation and only their SHA-256
lands in the database. Prefixes make it obvious what kind of principal a
token belongs to: usr_… for humans, agt_… for agents."""

import hashlib
import secrets

from . import db


def mint_token(prefix: str) -> tuple[str, str]:
    secret = f"{prefix}_{secrets.token_urlsafe(24)}"
    return secret, hashlib.sha256(secret.encode()).hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_from_token(token: str):
    with db.read() as cur:
        return cur.execute(
            "SELECT * FROM users WHERE token_hash=?", (hash_token(token),)
        ).fetchone()


def agent_from_token(token: str):
    with db.read() as cur:
        return cur.execute(
            "SELECT * FROM agents WHERE token_hash=?", (hash_token(token),)
        ).fetchone()
