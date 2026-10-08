"""Auth package: passwords, JWT session tokens, Google Sign-In, rate limiting,
and the `get_current_user` FastAPI dependency."""

from .deps import get_current_user, user_from_claims
from .google import (
    GoogleAuthError,
    GoogleIdentity,
    GoogleNotConfiguredError,
    verify_google_id_token,
)
from .passwords import (
    CredentialError,
    hash_password,
    needs_rehash,
    normalize_email,
    validate_password,
    verify_password,
)
from .ratelimit import LoginRateLimiter, RateLimited, login_limiter
from .tokens import (
    AuthConfigError,
    TokenClaims,
    TokenError,
    hash_token,
    mint_access_token,
    mint_refresh_token,
    verify_token,
)

__all__ = [
    "AuthConfigError",
    "CredentialError",
    "GoogleAuthError",
    "GoogleIdentity",
    "GoogleNotConfiguredError",
    "LoginRateLimiter",
    "RateLimited",
    "TokenClaims",
    "TokenError",
    "get_current_user",
    "hash_password",
    "hash_token",
    "login_limiter",
    "mint_access_token",
    "mint_refresh_token",
    "needs_rehash",
    "normalize_email",
    "user_from_claims",
    "validate_password",
    "verify_google_id_token",
    "verify_password",
    "verify_token",
]
