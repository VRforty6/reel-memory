"""FastAPI auth dependency: `get_current_user`.

Every product endpoint depends on this instead of the old single-user
`get_or_create_local_user`. Unauthenticated requests get a 401 with a
machine-readable body; the Android client treats that as "show the
sign-in screen".
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User

from .tokens import AuthConfigError, TokenClaims, TokenError, verify_token

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def user_from_claims(claims: TokenClaims, find_user: Callable) -> User:
    """Testable core: map verified claims to a user row (or 401).

    `find_user(user_id)` is the DB lookup, injected so tests stay offline.
    """
    user = find_user(claims.user_id)
    if user is None:
        raise _unauthorized("account_not_found", "this account no longer exists")
    return user


def get_current_user(
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("unauthorized", "sign in required")
    try:
        claims = verify_token(credentials.credentials, "access")
    except AuthConfigError as e:
        # Server misconfiguration — 500, not 401: the client can't fix this.
        raise HTTPException(status_code=500, detail=str(e)) from e
    except TokenError as e:
        raise _unauthorized(e.code, str(e)) from e
    return user_from_claims(claims, lambda uid: db.get(User, uid))


__all__ = ["get_current_user", "user_from_claims"]
