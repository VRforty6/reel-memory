"""Sign in with Google: server-side ID token verification.

The Android app sends the Google ID token it got from Google Sign-In; we
verify it here (signature via Google's JWKS, issuer, audience == our client
ID, expiry) and create-or-link the account. The client never sends us a
Google access token and we never call Google APIs on the user's behalf.

Transport is injectable (`http_get`) so tests stay offline — pass a stub
returning the JWKS JSON. The JWKS response is cached in-process for 1 hour.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import httpx
import jwt

from app.config import settings

JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_ALLOWED_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
_JWKS_TTL_S = 3600


class GoogleAuthError(ValueError):
    """Verification failed. `code` is machine-readable for the API layer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GoogleNotConfiguredError(RuntimeError):
    """GOOGLE_CLIENT_ID is not set."""


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str  # stable Google user id — the link key
    email: str
    email_verified: bool


# In-process JWKS cache: (fetched_at, keys). Per-process is fine — a stale
# cache entry just means one extra fetch after expiry; verification itself
# never trusts a cached key beyond its TTL.
_jwks_cache: tuple[float, dict] | None = None


def _default_http_get(url: str, timeout: float = 10.0) -> dict:
    resp = httpx.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise GoogleAuthError("jwks_error", "unexpected JWKS response")
    return data


def _get_jwks(http_get: Callable[..., dict] | None = None) -> dict:
    global _jwks_cache
    now = time.monotonic()
    if _jwks_cache is not None and now - _jwks_cache[0] < _JWKS_TTL_S:
        return _jwks_cache[1]
    getter = http_get or _default_http_get
    try:
        jwks = getter(JWKS_URL)
    except GoogleAuthError:
        raise
    except Exception as e:  # httpx errors, JSON errors — never leak internals
        raise GoogleAuthError("jwks_error", "could not fetch Google signing keys") from e
    if not isinstance(jwks, dict) or not jwks.get("keys"):
        raise GoogleAuthError("jwks_error", "Google signing keys unavailable")
    _jwks_cache = (now, jwks)
    return jwks


def _key_for(jwks: dict, kid: str | None):
    """Pick the JWK matching the token's kid (or the single key if only one)."""
    keys = jwks.get("keys", [])
    if kid:
        for k in keys:
            if k.get("kid") == kid:
                return jwt.algorithms.RSAAlgorithm.from_jwk(k)
    if len(keys) == 1:
        return jwt.algorithms.RSAAlgorithm.from_jwk(keys[0])
    raise GoogleAuthError("invalid_token", "no matching Google signing key")


def verify_google_id_token(
    id_token: str,
    client_id: str | None = None,
    http_get: Callable[..., dict] | None = None,
) -> GoogleIdentity:
    """Verify a Google ID token and return the identity it asserts.

    Raises GoogleNotConfiguredError when GOOGLE_CLIENT_ID is unset, or
    GoogleAuthError(code) for any verification failure.
    """
    cid = client_id or settings.google_client_id
    if not cid:
        raise GoogleNotConfiguredError(
            "GOOGLE_CLIENT_ID is not set — Google sign-in is unavailable "
            "(see .env.example)"
        )
    if not id_token or not isinstance(id_token, str):
        raise GoogleAuthError("invalid_token", "missing Google ID token")

    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.InvalidTokenError as e:
        raise GoogleAuthError("invalid_token", "malformed Google ID token") from e
    if header.get("alg") != "RS256":
        # Reject alg confusion outright — Google only ever signs RS256.
        raise GoogleAuthError("invalid_token", "unexpected signing algorithm")

    key = _key_for(_get_jwks(http_get), header.get("kid"))
    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=cid,
            issuer=_ALLOWED_ISSUERS,
            options={"require": ["exp", "iss", "aud", "sub", "email"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise GoogleAuthError("token_expired", "Google sign-in expired — try again") from e
    except jwt.InvalidTokenError as e:
        raise GoogleAuthError("invalid_token", "Google ID token failed verification") from e

    email = str(claims.get("email", "")).strip().lower()
    if not email:
        raise GoogleAuthError("invalid_token", "Google token has no email")
    return GoogleIdentity(
        sub=str(claims["sub"]),
        email=email,
        email_verified=bool(claims.get("email_verified", False)),
    )


def clear_jwks_cache() -> None:
    """Test helper: reset the in-process JWKS cache."""
    global _jwks_cache
    _jwks_cache = None
