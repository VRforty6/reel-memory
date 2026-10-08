"""JWT session tokens (HS256). Pure functions (unit-testable).

Two token kinds:
- access:  short-lived (default 15 min), sent as `Authorization: Bearer`.
- refresh: long-lived (default 30 days); only its sha256 is stored in the
  `refresh_tokens` table, and each refresh rotates (revokes + reissues).

The secret comes from settings.jwt_secret and must be set in production —
mint/verify fail fast with AuthConfigError when it is missing, never
falling back to a weak default.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings

ALGORITHM = "HS256"
_ISSUER = "reel-memory"


class AuthConfigError(RuntimeError):
    """JWT_SECRET (or equivalent) is not configured."""


class TokenError(ValueError):
    """Token invalid, expired, or of the wrong kind. `code` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _secret() -> str:
    secret = settings.jwt_secret
    if not secret:
        raise AuthConfigError(
            "JWT_SECRET is not set — set it in the environment (see .env.example). "
            "Auth endpoints refuse to run without it."
        )
    return secret


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TokenClaims:
    user_id: uuid.UUID
    kind: str  # "access" | "refresh"
    jti: str
    expires_at: datetime


def mint_access_token(user_id: uuid.UUID, ttl_s: int | None = None) -> str:
    now = _utcnow()
    payload = {
        "iss": _ISSUER,
        "sub": str(user_id),
        "kind": "access",
        "jti": secrets.token_hex(16),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_s or settings.jwt_access_ttl_s)).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def mint_refresh_token(user_id: uuid.UUID, ttl_s: int | None = None) -> tuple[str, TokenClaims]:
    """Returns (token, claims). The caller stores sha256(token) + jti server-side."""
    now = _utcnow()
    jti = secrets.token_hex(16)
    exp = now + timedelta(seconds=ttl_s or settings.jwt_refresh_ttl_s)
    payload = {
        "iss": _ISSUER,
        "sub": str(user_id),
        "kind": "refresh",
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, _secret(), algorithm=ALGORITHM)
    return token, TokenClaims(user_id=user_id, kind="refresh", jti=jti, expires_at=exp)


def hash_token(token: str) -> str:
    """sha256 of a refresh token — what's stored in the DB, never the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, expected_kind: str) -> TokenClaims:
    """Verify signature/expiry/issuer/kind. Raises TokenError on any problem."""
    if not token or not isinstance(token, str):
        raise TokenError("invalid_token", "missing or malformed token")
    try:
        payload = jwt.decode(
            token,
            _secret(),
            algorithms=[ALGORITHM],
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenError("token_expired", "token has expired — sign in again") from e
    except jwt.InvalidTokenError as e:
        raise TokenError("invalid_token", "token is invalid") from e
    if payload.get("kind") != expected_kind:
        raise TokenError("invalid_token", f"expected a {expected_kind} token")
    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except ValueError as e:
        raise TokenError("invalid_token", "token subject is not a user id") from e
    exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    return TokenClaims(user_id=user_id, kind=payload["kind"], jti=payload["jti"], expires_at=exp)
