"""Auth API — PRD commercialization track.

POST /v1/auth/signup   {email, password} -> 201 TokenPair
POST /v1/auth/login    {email, password} -> TokenPair (rate-limited)
POST /v1/auth/refresh  {refresh_token}   -> TokenPair (rotates)
POST /v1/auth/logout   {refresh_token}   -> 204 (revokes)
POST /v1/auth/google   {id_token}        -> TokenPair (create-or-link)

All credential inputs are treated as hostile: emails normalized + validated,
passwords policy-checked and Argon2id-hashed, login throttled per-IP and
per-account, and failures never reveal whether an email is registered.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.auth import (
    AuthConfigError,
    CredentialError,
    RateLimited,
    TokenError,
    GoogleAuthError,
    GoogleNotConfiguredError,
    hash_password,
    hash_token,
    login_limiter,
    mint_access_token,
    mint_refresh_token,
    needs_rehash,
    normalize_email,
    validate_password,
    verify_google_id_token,
    verify_password,
    verify_token,
)
from app.config import settings
from app.db import get_db
from app.models import RefreshToken, User
from app.schemas import (
    GoogleSignInRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    SignupRequest,
    TokenPair,
)

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _client_ip(request: Request) -> str:
    # Behind a proxy, prefer X-Forwarded-For's leftmost entry — but only the
    # first hop we can't trust anyway; for throttling purposes the direct
    # peer is the honest signal. Use client.host (the TCP peer).
    return request.client.host if request.client else "unknown"


def _auth_config_error(e: AuthConfigError) -> HTTPException:
    return HTTPException(status_code=500, detail=str(e))


def _issue_pair(db: Session, user: User) -> TokenPair:
    """Mint an access + refresh pair and persist the refresh record."""
    try:
        access = mint_access_token(user.id)
        refresh, claims = mint_refresh_token(user.id)
    except AuthConfigError as e:
        raise _auth_config_error(e) from e
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh),
            jti=claims.jti,
            expires_at=claims.expires_at,
        )
    )
    db.commit()
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.jwt_access_ttl_s,
    )


def _invalid_credentials() -> HTTPException:
    # Same message whether the email is unknown, has no password, or the
    # password is wrong — no account-enumeration oracle.
    return HTTPException(
        status_code=401,
        detail={"code": "invalid_credentials", "message": "wrong email or password"},
    )


@router.post("/v1/auth/signup", response_model=TokenPair, status_code=201)
def signup(req: SignupRequest, db: Session = Depends(get_db)) -> TokenPair:
    try:
        email = normalize_email(req.email)
        validate_password(req.password)
    except CredentialError as e:
        raise HTTPException(
            status_code=422, detail={"code": e.code, "message": str(e)}
        ) from e

    if db.query(User).filter(User.email == email).first() is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "email_taken", "message": "an account with this email already exists"},
        )

    user = User(email=email, password_hash=hash_password(req.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return _issue_pair(db, user)


@router.post("/v1/auth/login", response_model=TokenPair)
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    ip = _client_ip(request)
    try:
        email = normalize_email(req.email)
    except CredentialError:
        # Don't leak email validity; still count it against the IP bucket.
        login_limiter.record_failure(ip, "invalid")
        raise _invalid_credentials()

    try:
        login_limiter.check(ip, email)
    except RateLimited as e:
        raise HTTPException(
            status_code=429,
            detail={"code": "too_many_attempts", "message": str(e)},
            headers={"Retry-After": str(e.retry_after_s)},
        ) from e

    user = db.query(User).filter(User.email == email).first()
    ok = verify_password(user.password_hash if user else None, req.password)
    if not ok:
        login_limiter.record_failure(ip, email)
        raise _invalid_credentials()

    login_limiter.record_success(ip, email)
    # Transparently upgrade stale hashes after a successful verification.
    if user.password_hash and needs_rehash(user.password_hash):
        user.password_hash = hash_password(req.password)
        db.commit()
    return _issue_pair(db, user)


def _load_refresh_record(db: Session, raw_token: str) -> RefreshToken:
    try:
        claims = verify_token(raw_token, "refresh")
    except (TokenError, AuthConfigError) as e:
        code = "token_expired" if isinstance(e, TokenError) and e.code == "token_expired" else "invalid_token"
        raise HTTPException(
            status_code=401, detail={"code": code, "message": str(e)}
        ) from e
    record = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == hash_token(raw_token))
        .first()
    )
    if (
        record is None
        or record.revoked
        or record.expires_at <= _utcnow()
        or record.jti != claims.jti
    ):
        # A revoked-but-presented token means possible reuse after rotation:
        # fail closed. (Theft detection beyond this is a future step.)
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_token", "message": "refresh token is invalid or revoked"},
        )
    return record


@router.post("/v1/auth/refresh", response_model=TokenPair)
def refresh(req: RefreshRequest, db: Session = Depends(get_db)) -> TokenPair:
    record = _load_refresh_record(db, req.refresh_token)
    user = db.get(User, record.user_id)
    if user is None:
        raise HTTPException(
            status_code=401, detail={"code": "invalid_token", "message": "account not found"}
        )
    # Rotation: the presented token dies here; the new pair takes over.
    record.revoked = True
    db.commit()
    return _issue_pair(db, user)


@router.post("/v1/auth/logout", status_code=204)
def logout(req: LogoutRequest, db: Session = Depends(get_db)) -> Response:
    record = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == hash_token(req.refresh_token))
        .first()
    )
    if record is not None and not record.revoked:
        record.revoked = True
        db.commit()
    # Idempotent: unknown/already-revoked tokens still return 204.
    return Response(status_code=204)


@router.post("/v1/auth/google", response_model=TokenPair)
def google_sign_in(req: GoogleSignInRequest, db: Session = Depends(get_db)) -> TokenPair:
    try:
        identity = verify_google_id_token(req.id_token)
    except GoogleNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except GoogleAuthError as e:
        raise HTTPException(
            status_code=401, detail={"code": e.code, "message": str(e)}
        ) from e

    if not identity.email_verified:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "email_not_verified",
                "message": "your Google email isn't verified — verify it with Google first",
            },
        )

    user = db.query(User).filter(User.google_sub == identity.sub).first()
    if user is None:
        # Link-or-create by email: a matching password account gets the
        # Google identity attached (email_verified by Google == proof).
        user = db.query(User).filter(User.email == identity.email).first()
        if user is None:
            user = User(email=identity.email, google_sub=identity.sub)
            db.add(user)
        else:
            user.google_sub = identity.sub
        db.commit()
        db.refresh(user)
    return _issue_pair(db, user)
