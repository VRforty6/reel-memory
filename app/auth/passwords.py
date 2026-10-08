"""Password hashing and credential policy. Pure functions (unit-testable).

Uses Argon2id (argon2-cffi) — memory-hard, the current recommended choice
for password storage. Plaintext passwords never leave the request handler.
"""

from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

_ph = PasswordHasher()  # OWASP-sane defaults (argon2id, m=64MiB, t=3, p=4)

# A single dummy hash used when the email doesn't exist, so a login attempt
# for an unknown account costs ~the same as a real verification (no cheap
# user-enumeration oracle via timing).
_DUMMY_HASH = _ph.hash("dummy-password-for-timing-parity-only")

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

MIN_PASSWORD_LEN = 10
MAX_PASSWORD_LEN = 128


class CredentialError(ValueError):
    """Raised for policy violations (weak password, bad email). Carries a
    machine-readable `code` for the API layer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize_email(email: str) -> str:
    """Lowercase + strip; validates shape. Raises CredentialError("invalid_email")."""
    cleaned = (email or "").strip().lower()
    if len(cleaned) > 320 or not _EMAIL_RE.match(cleaned):
        raise CredentialError("invalid_email", "that doesn't look like an email address")
    return cleaned


def validate_password(password: str) -> None:
    """Enforce the password policy. Raises CredentialError("weak_password")."""
    if not isinstance(password, str):
        raise CredentialError("weak_password", "password must be a string")
    if not (MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN):
        raise CredentialError(
            "weak_password",
            f"password must be {MIN_PASSWORD_LEN}-{MAX_PASSWORD_LEN} characters",
        )


def hash_password(password: str) -> str:
    """Argon2id hash (includes random salt + params)."""
    return _ph.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-work verification.

    When `password_hash` is None (unknown email) we verify against a dummy
    hash and return False — same cost, no enumeration oracle.
    """
    candidate = password_hash or _DUMMY_HASH
    try:
        _ph.verify(candidate, password)
    except (VerifyMismatchError, InvalidHash):
        return False
    # Correct password but stale params (e.g. after we harden settings):
    # the API layer rehashes on successful login via needs_rehash().
    return password_hash is not None


def needs_rehash(password_hash: str) -> bool:
    """True when a valid hash was made with outdated parameters."""
    try:
        return _ph.check_needs_rehash(password_hash)
    except InvalidHash:
        return False
