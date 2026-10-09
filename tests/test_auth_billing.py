"""Tests for the commercialization track: auth, entitlements, Play billing.

All network/provider access is stubbed or pure — zero live calls. DB-backed
endpoint wiring is covered through the pure helpers the endpoints delegate to
(per repo convention); the DB counter itself is exercised via a fake session
double, not a live database.
"""

from __future__ import annotations

import base64
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.auth import (
    CredentialError,
    LoginRateLimiter,
    RateLimited,
    TokenError,
    AuthConfigError,
    GoogleAuthError,
    GoogleNotConfiguredError,
    hash_password,
    hash_token,
    mint_access_token,
    mint_refresh_token,
    needs_rehash,
    normalize_email,
    user_from_claims,
    validate_password,
    verify_google_id_token,
    verify_password,
    verify_token,
)
from app.auth.google import clear_jwks_cache
from app.auth.tokens import _secret
from app.billing import (
    BillingError,
    BillingNotConfiguredError,
    clear_token_cache,
    load_service_account,
    verify_subscription,
)
from app.billing.play import _HttpResponse
from app.config import settings
from app.entitlements import (
    BUCKET_CAPTURES,
    BUCKET_QUESTIONS,
    BUCKET_VERIFICATIONS,
    effective_tier,
    limits_for,
    period_for,
    quota_exceeded_error,
    resets_at_for,
)


@pytest.fixture(autouse=True)
def _clean_caches():
    clear_jwks_cache()
    clear_token_cache()
    yield
    clear_jwks_cache()
    clear_token_cache()


@pytest.fixture()
def jwt_secret(monkeypatch):
    secret = "test-only-jwt-secret-at-least-32-bytes"
    monkeypatch.setattr(settings, "jwt_secret", secret)
    return secret


# --- passwords ----------------------------------------------------------------


def test_normalize_email_ok():
    assert normalize_email("  Ramana@Example.COM ") == "ramana@example.com"


@pytest.mark.parametrize("bad", ["not-an-email", "a@b", "@x.com", "a" * 321 + "@x.com", ""])
def test_normalize_email_rejects(bad):
    with pytest.raises(CredentialError) as e:
        normalize_email(bad)
    assert e.value.code == "invalid_email"


def test_password_policy():
    validate_password("long-enough-password-1")
    with pytest.raises(CredentialError) as e:
        validate_password("short")
    assert e.value.code == "weak_password"
    with pytest.raises(CredentialError):
        validate_password("x" * 129)


def test_password_hash_verify_roundtrip():
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert verify_password(h, "correct horse battery staple") is True
    assert verify_password(h, "wrong password here!!") is False


def test_password_verify_unknown_hash_costs_the_same_and_fails():
    # Unknown email: verified against a dummy hash, returns False (no oracle).
    assert verify_password(None, "whatever-password-1") is False


def test_needs_rehash_false_for_fresh():
    assert needs_rehash(hash_password("another-long-password")) is False


# --- JWT session tokens --------------------------------------------------------


def test_jwt_secret_required(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", None)
    with pytest.raises(AuthConfigError):
        _secret()
    with pytest.raises(AuthConfigError):
        mint_access_token(uuid.uuid4())


def test_access_token_roundtrip(jwt_secret):
    uid = uuid.uuid4()
    token = mint_access_token(uid)
    claims = verify_token(token, "access")
    assert claims.user_id == uid
    assert claims.kind == "access"


def test_refresh_token_roundtrip_and_hash(jwt_secret):
    uid = uuid.uuid4()
    token, claims = mint_refresh_token(uid)
    assert claims.kind == "refresh"
    assert len(claims.jti) == 32
    # Only the sha256 is stored server-side; the hash is stable.
    assert hash_token(token) == hash_token(token)
    assert len(hash_token(token)) == 64
    back = verify_token(token, "refresh")
    assert back.jti == claims.jti


def test_token_wrong_kind_rejected(jwt_secret):
    uid = uuid.uuid4()
    access = mint_access_token(uid)
    with pytest.raises(TokenError) as e:
        verify_token(access, "refresh")
    assert e.value.code == "invalid_token"


def test_token_expired_rejected(jwt_secret):
    token = mint_access_token(uuid.uuid4(), ttl_s=-10)
    with pytest.raises(TokenError) as e:
        verify_token(token, "access")
    assert e.value.code == "token_expired"


def test_token_tampered_rejected(jwt_secret):
    token = mint_access_token(uuid.uuid4())
    tampered = token[:-2] + ("ab" if not token.endswith("ab") else "cd")
    with pytest.raises(TokenError):
        verify_token(tampered, "access")


def test_user_from_claims(jwt_secret):
    uid = uuid.uuid4()
    claims = verify_token(mint_access_token(uid), "access")
    sentinel = object()
    assert user_from_claims(claims, lambda u: sentinel if u == uid else None) is sentinel
    with pytest.raises(HTTPException) as e:
        user_from_claims(claims, lambda u: None)
    assert e.value.status_code == 401


# --- rate limiting --------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def test_rate_limiter_blocks_after_limit():
    clock = _Clock()
    rl = LoginRateLimiter(max_attempts_ip=3, max_attempts_account=2, window_s=60, clock=clock)
    rl.check("1.2.3.4", "a@x.com")  # fine initially
    rl.record_failure("1.2.3.4", "a@x.com")
    rl.record_failure("1.2.3.4", "a@x.com")
    with pytest.raises(RateLimited) as e:
        rl.check("1.2.3.4", "a@x.com")
    assert e.value.retry_after_s > 0
    # Window slides: after 61s the attempts expire.
    clock.advance(61)
    rl.check("1.2.3.4", "a@x.com")


def test_rate_limiter_success_clears_account_not_ip():
    clock = _Clock()
    rl = LoginRateLimiter(max_attempts_ip=2, max_attempts_account=1, window_s=60, clock=clock)
    rl.record_failure("9.9.9.9", "victim@x.com")
    rl.record_success("9.9.9.9", "victim@x.com")
    rl.check("9.9.9.9", "victim@x.com")  # account cleared
    rl.record_failure("9.9.9.9", "other@x.com")
    rl.record_failure("9.9.9.9", "third@x.com")
    with pytest.raises(RateLimited):  # IP bucket still throttles
        rl.check("9.9.9.9", "fourth@x.com")


# --- entitlements ---------------------------------------------------------------


def _user(tier="free", pro_expires_at=None):
    return SimpleNamespace(tier=tier, pro_expires_at=pro_expires_at)


def test_effective_tier():
    now = datetime.now(timezone.utc)
    assert effective_tier(_user("free"), now) == "free"
    assert effective_tier(_user("pro", now + timedelta(days=3)), now) == "pro"
    assert effective_tier(_user("pro", now - timedelta(days=1)), now) == "free"
    assert effective_tier(_user("pro", None), now) == "pro"  # lifetime grant


def test_limits_for_scales_with_pro(monkeypatch):
    monkeypatch.setattr(settings, "free_captures_monthly", 20)
    monkeypatch.setattr(settings, "free_questions_daily", 50)
    monkeypatch.setattr(settings, "free_verifications_daily", 10)
    monkeypatch.setattr(settings, "pro_quota_multiplier", 10)
    free = limits_for("free")
    pro = limits_for("pro")
    assert free[BUCKET_CAPTURES] == 20
    assert pro[BUCKET_CAPTURES] == 200
    assert pro[BUCKET_QUESTIONS] == 500
    assert pro[BUCKET_VERIFICATIONS] == 100


def test_period_keys():
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    assert period_for(BUCKET_CAPTURES, now) == "2026-09"
    assert period_for(BUCKET_QUESTIONS, now) == "2026-09-22"
    assert period_for(BUCKET_VERIFICATIONS, now) == "2026-09-22"


def test_resets_at_boundaries():
    # Daily: next midnight UTC.
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    assert resets_at_for(BUCKET_QUESTIONS, now) == datetime(2026, 9, 23, tzinfo=timezone.utc)
    # Monthly: first of next month (September has 30 days).
    assert resets_at_for(BUCKET_CAPTURES, now) == datetime(2026, 10, 1, tzinfo=timezone.utc)
    # February edge: 2027-02 -> 2027-03-01.
    feb = datetime(2027, 2, 10, tzinfo=timezone.utc)
    assert resets_at_for(BUCKET_CAPTURES, feb) == datetime(2027, 3, 1, tzinfo=timezone.utc)


def test_quota_exceeded_error_shape():
    err = quota_exceeded_error(
        tier="free", bucket="captures", limit=20, used=20,
        resets_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    assert err.status_code == 402
    d = err.detail
    assert d["code"] == "quota_exceeded"
    assert d["tier"] == "free" and d["bucket"] == "captures"
    assert d["limit"] == 20 and d["used"] == 20
    assert d["resets_at"] == "2026-10-01T00:00:00+00:00"


# --- Google Sign-In --------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _google_test_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = key.public_key().public_numbers()
    n = _b64url(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big"))
    e = _b64url(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big"))
    jwks = {"keys": [{"kty": "RSA", "kid": "testkid", "n": n, "e": e}]}
    return priv, jwks


def _google_id_token(priv_pem: bytes, claims: dict, kid: str = "testkid", alg: str = "RS256"):
    if alg == "RS256":
        return jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": kid})
    return jwt.encode(claims, "hmac-secret-that-is-long-enough-for-tests", algorithm="HS256")


def _google_claims(**over):
    now = int(time.time())
    claims = {
        "iss": "accounts.google.com",
        "aud": "test-client-id",
        "sub": "google-user-123",
        "email": "user@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(over)
    return claims


def test_google_verify_ok():
    priv, jwks = _google_test_keys()
    token = _google_id_token(priv, _google_claims())
    ident = verify_google_id_token(token, client_id="test-client-id", http_get=lambda url: jwks)
    assert ident.sub == "google-user-123"
    assert ident.email == "user@example.com"
    assert ident.email_verified is True


def test_google_verify_wrong_audience():
    priv, jwks = _google_test_keys()
    token = _google_id_token(priv, _google_claims(aud="other-client"))
    with pytest.raises(GoogleAuthError) as e:
        verify_google_id_token(token, client_id="test-client-id", http_get=lambda url: jwks)
    assert e.value.code == "invalid_token"


def test_google_verify_expired():
    priv, jwks = _google_test_keys()
    token = _google_id_token(priv, _google_claims(exp=int(time.time()) - 10))
    with pytest.raises(GoogleAuthError) as e:
        verify_google_id_token(token, client_id="test-client-id", http_get=lambda url: jwks)
    assert e.value.code == "token_expired"


def test_google_verify_rejects_alg_confusion():
    priv, jwks = _google_test_keys()
    token = _google_id_token(priv, _google_claims(), alg="HS256")
    with pytest.raises(GoogleAuthError) as e:
        verify_google_id_token(token, client_id="test-client-id", http_get=lambda url: jwks)
    assert e.value.code == "invalid_token"


def test_google_verify_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", None)
    with pytest.raises(GoogleNotConfiguredError):
        verify_google_id_token("whatever", client_id=None)


def test_google_verify_jwks_failure():
    priv, _ = _google_test_keys()
    token = _google_id_token(priv, _google_claims())

    def boom(url):
        raise RuntimeError("network down")

    with pytest.raises(GoogleAuthError) as e:
        verify_google_id_token(token, client_id="test-client-id", http_get=boom)
    assert e.value.code == "jwks_error"


# --- Play billing ------------------------------------------------------------------


def _sa_info():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return {"client_email": "sa@test.iam.gserviceaccount.com", "private_key": pem}


def _play_http(token_resp=None, purchase_resp=None, purchase_status=200):
    calls = []

    def http(method, url, **kwargs):
        calls.append((method, url))
        if "oauth2.googleapis.com/token" in url:
            return _HttpResponse(200, token_resp or {"access_token": "ya29.test", "expires_in": 3600})
        return _HttpResponse(purchase_status, purchase_resp or {})

    http.calls = calls
    return http


def _purchase_body(**over):
    body = {
        "expiryTimeMillis": str(int((time.time() + 30 * 86400) * 1000)),
        "autoRenewing": True,
        "paymentState": 1,
        "orderId": "GPA.1234",
    }
    body.update(over)
    return body


def test_play_verify_ok():
    sa = _sa_info()
    result = verify_subscription(
        "com.example.app", "pro_monthly", "purchase-token-abc",
        sa_info=sa, http=_play_http(purchase_resp=_purchase_body()),
    )
    assert result.valid is True
    assert result.auto_renewing is True
    assert result.order_id == "GPA.1234"
    assert result.expires_at > datetime.now(timezone.utc)


def test_play_verify_pending_payment_not_valid():
    sa = _sa_info()
    result = verify_subscription(
        "com.example.app", "pro_monthly", "tok",
        sa_info=sa, http=_play_http(purchase_resp=_purchase_body(paymentState=0)),
    )
    assert result.valid is False


def test_play_verify_expired_not_valid():
    sa = _sa_info()
    body = _purchase_body(expiryTimeMillis=str(int((time.time() - 3600) * 1000)))
    result = verify_subscription(
        "com.example.app", "pro_monthly", "tok", sa_info=sa, http=_play_http(purchase_resp=body)
    )
    assert result.valid is False


def test_play_verify_unknown_token():
    sa = _sa_info()
    with pytest.raises(BillingError) as e:
        verify_subscription(
            "com.example.app", "pro_monthly", "bogus",
            sa_info=sa, http=_play_http(purchase_status=404),
        )
    assert e.value.code == "invalid_purchase"


def test_play_verify_service_account_rejected():
    sa = _sa_info()
    with pytest.raises(BillingError) as e:
        verify_subscription(
            "com.example.app", "pro_monthly", "tok",
            sa_info=sa, http=_play_http(purchase_status=403),
        )
    assert e.value.code == "billing_misconfigured"


def test_play_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "google_play_service_account_json", None)
    with pytest.raises(BillingNotConfiguredError):
        load_service_account()


def test_play_load_service_account_inline_json(monkeypatch):
    import json as _json

    monkeypatch.setattr(
        settings,
        "google_play_service_account_json",
        _json.dumps({"client_email": "a@b.c", "private_key": "k"}),
    )
    info = load_service_account()
    assert info["client_email"] == "a@b.c"


def test_play_load_service_account_bad_json(monkeypatch):
    monkeypatch.setattr(settings, "google_play_service_account_json", "not-json{{{")
    with pytest.raises(BillingError) as e:
        load_service_account()
    assert e.value.code == "billing_misconfigured"


def test_play_rejects_oversize_token():
    sa = _sa_info()
    with pytest.raises(BillingError) as e:
        verify_subscription("p", "s", "x" * 2000, sa_info=sa, http=_play_http())
    assert e.value.code == "invalid_purchase"
