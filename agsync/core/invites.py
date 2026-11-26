"""Secure HMAC-signed Join Link invites and per-agent Bearer token management."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

from agsync.core.errors import ConflictError, NotFoundError, ValidationError
from agsync.db import SyncDatabase
from agsync.protocol import validate_agent_id


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sign_nonce(nonce: str, agent_id: str, expires_at: float, secret: str) -> str:
    msg = f"{nonce}:{agent_id}:{int(expires_at)}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


class InviteService:
    """Atomic single-use invite nonces and per-agent Bearer tokens."""

    def __init__(self, db: SyncDatabase) -> None:
        self.db = db

    def create_invite(
        self,
        agent_id: str,
        *,
        secret: str,
        ttl_sec: int = 900,
        created_by: str = "host",
    ) -> dict[str, Any]:
        aid = validate_agent_id(agent_id)
        if ttl_sec <= 0 or ttl_sec > 86400:
            raise ValidationError("validation_error", "ttl_sec must be between 1 and 86400")
        now = float(self.db.now_fn())
        expires_at = float(int(now + ttl_sec))
        nonce = secrets.token_urlsafe(18)
        sig = _sign_nonce(nonce, aid, expires_at, secret)

        with self.db.write_tx():
            self.db.conn.execute(
                """
                INSERT INTO invites (nonce, agent_id, created_by, created_at, expires_at, used, used_at)
                VALUES (?, ?, ?, ?, ?, 0, NULL);
                """,
                (nonce, aid, created_by, now, expires_at),
            )
        return {
            "nonce": nonce,
            "agent_id": aid,
            "expires_at": expires_at,
            "signature": sig,
        }

    def consume_invite(
        self,
        nonce: str,
        signature: str,
        *,
        secret: str,
    ) -> dict[str, Any]:
        now = float(self.db.now_fn())
        with self.db.write_tx():
            row = self.db.conn.execute(
                "SELECT * FROM invites WHERE nonce = ?;", (nonce,)
            ).fetchone()
            if not row:
                raise NotFoundError("invite_not_found", "Invite nonce not found")
            aid = str(row["agent_id"])
            exp = float(row["expires_at"])
            expected_sig = _sign_nonce(nonce, aid, exp, secret)
            if not hmac.compare_digest(signature.encode("utf-8"), expected_sig.encode("utf-8")):
                raise ValidationError("invalid_signature", "Invalid invite signature")
            if now > exp:
                raise ConflictError("invite_expired", "Invite link has expired")

            cur = self.db.conn.execute(
                "UPDATE invites SET used = 1, used_at = ? WHERE nonce = ? AND used = 0 AND expires_at >= ?;",
                (now, nonce, now),
            )
            if cur.rowcount != 1:
                raise ConflictError("invite_already_used", "Invite link has already been consumed")

            agent_token = f"agt_{secrets.token_urlsafe(24)}"
            self.db.conn.execute(
                """
                INSERT INTO agent_tokens (token_hash, agent_id, role, created_at, revoked)
                VALUES (?, ?, 'agent', ?, 0)
                ON CONFLICT(agent_id) DO UPDATE SET
                    token_hash = excluded.token_hash,
                    created_at = excluded.created_at,
                    revoked = 0;
                """,
                (_hash_token(agent_token), aid, now),
            )
        return {"agent_id": aid, "token": agent_token, "expires_at": exp}

    def verify_agent_token(self, provided_token: str) -> str | None:
        """Return agent_id if provided_token matches an issued per-agent token, else None."""
        if not provided_token:
            return None
        th = _hash_token(provided_token)
        with self.db.read_tx():
            row = self.db.conn.execute(
                "SELECT agent_id FROM agent_tokens WHERE token_hash = ? AND revoked = 0;", (th,)
            ).fetchone()
        return str(row["agent_id"]) if row else None
