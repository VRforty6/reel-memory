"""Google Play Billing verification (server-side).

Flow: the Android app completes a subscription purchase with Play Billing
and sends us {package_name, product_id, purchase_token}. We verify the token
against the Google Play Developer API using a service account, and grant
Pro until the subscription's expiryTimeMillis. Renewals, cancellations and
expiry are handled by re-verification — the client re-sends the purchase
token (e.g. on app start) and we refresh the entitlement.

Transport is injectable (`http`) so tests stay offline. When the service
account is not configured the API layer fails with 503 billing_not_configured
— pro is NEVER granted without a successful Google verification.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx
import jwt

from app.config import settings

ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_PURCHASES_URL = (
    "https://androidpublisher.googleapis.com/androidpublisher/v3"
    "/applications/{package_name}/purchases/subscriptions/{product_id}/tokens/{token}"
)
_ACCESS_TOKEN_TTL_SKEW = 60  # refresh a minute before expiry


class BillingNotConfiguredError(RuntimeError):
    """GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is not set."""


class BillingError(ValueError):
    """Verification failed. `code` is machine-readable for the API layer:

    - invalid_purchase:      token unknown/expired at Google (client problem)
    - billing_misconfigured: service account rejected (publisher must fix access)
    - billing_unavailable:   Google API error (transient)
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlayVerification:
    valid: bool
    expires_at: datetime | None  # from expiryTimeMillis
    auto_renewing: bool
    order_id: str | None
    payment_state: int | None


class _HttpResponse:
    """Minimal response surface the injectable transport must provide."""

    def __init__(self, status_code: int, payload: dict | None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def _default_http(method: str, url: str, **kwargs) -> _HttpResponse:
    timeout = kwargs.pop("timeout", 15.0)
    resp = httpx.request(method, url, timeout=timeout, **kwargs)
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    return _HttpResponse(resp.status_code, payload if isinstance(payload, dict) else {})


# In-process access-token cache: client_email -> (token, expires_at_monotonic).
_token_cache: dict[str, tuple[str, float]] = {}


def load_service_account() -> dict:
    """Parse GOOGLE_PLAY_SERVICE_ACCOUNT_JSON (inline JSON or a file path).

    Raises BillingNotConfiguredError when unset; BillingError("billing_misconfigured")
    when the value isn't usable JSON with the required fields.
    """
    raw = settings.google_play_service_account_json
    if not raw:
        raise BillingNotConfiguredError(
            "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is not set — Play purchase "
            "verification is unavailable (see README for setup steps)"
        )
    text = raw.strip()
    if os.path.exists(text):
        with open(text, "r", encoding="utf-8") as f:
            text = f.read()
    try:
        info = json.loads(text)
    except (ValueError, OSError) as e:
        raise BillingError(
            "billing_misconfigured",
            "the Play service-account value is not valid JSON",
        ) from e
    for field in ("client_email", "private_key"):
        if not info.get(field):
            raise BillingError(
                "billing_misconfigured",
                f"the Play service account is missing {field!r}",
            )
    return info


def _service_account_access_token(
    sa_info: dict, http: Callable[..., _HttpResponse] | None = None
) -> str:
    """OAuth2 JWT-bearer flow: exchange a signed assertion for an access token."""
    client_email = sa_info["client_email"]
    now = time.time()
    cached = _token_cache.get(client_email)
    if cached and cached[1] > now + _ACCESS_TOKEN_TTL_SKEW:
        return cached[0]

    token_uri = sa_info.get("token_uri", _DEFAULT_TOKEN_URI)
    assertion = jwt.encode(
        {
            "iss": client_email,
            "scope": ANDROID_PUBLISHER_SCOPE,
            "aud": token_uri,
            "iat": int(now),
            "exp": int(now + 3600),
        },
        sa_info["private_key"],
        algorithm="RS256",
    )
    do_http = http or _default_http
    try:
        resp = do_http(
            "POST",
            token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
    except Exception as e:
        raise BillingError(
            "billing_unavailable", "could not reach Google's token endpoint"
        ) from e
    body = resp.json()
    if resp.status_code != 200 or not body.get("access_token"):
        raise BillingError(
            "billing_misconfigured",
            "Google rejected the service account — check the key and that the "
            "Google Play Android Developer API is enabled",
        )
    token = body["access_token"]
    _token_cache[client_email] = (token, now + int(body.get("expires_in", 3600)))
    return token


def verify_subscription(
    package_name: str,
    product_id: str,
    purchase_token: str,
    sa_info: dict | None = None,
    http: Callable[..., _HttpResponse] | None = None,
) -> PlayVerification:
    """Verify a Play subscription purchase token with Google.

    Returns PlayVerification; `valid` is True only when Google reports the
    subscription and its expiry is in the future with payment received/trial.
    Raises BillingNotConfiguredError / BillingError on failures.
    """
    info = sa_info if sa_info is not None else load_service_account()
    if not package_name or not product_id or not purchase_token:
        raise BillingError("invalid_purchase", "package, product and token are required")
    if len(purchase_token) > 1024:
        raise BillingError("invalid_purchase", "purchase token is malformed")

    access_token = _service_account_access_token(info, http)
    url = _PURCHASES_URL.format(
        package_name=package_name, product_id=product_id, token=purchase_token
    )
    do_http = http or _default_http
    try:
        resp = do_http(
            "GET", url, headers={"Authorization": f"Bearer {access_token}"}
        )
    except Exception as e:
        raise BillingError(
            "billing_unavailable", "could not reach the Play Developer API"
        ) from e

    if resp.status_code in (404, 410):
        raise BillingError(
            "invalid_purchase",
            "Google has no record of this purchase — the token is invalid or expired",
        )
    if resp.status_code in (401, 403):
        # Drop the cached token: it may just be stale. The API layer surfaces
        # misconfigured (not retried silently).
        _token_cache.pop(info["client_email"], None)
        raise BillingError(
            "billing_misconfigured",
            "the Play service account was rejected — grant it access in "
            "Play Console (see README)",
        )
    if resp.status_code != 200:
        raise BillingError(
            "billing_unavailable",
            f"the Play Developer API returned {resp.status_code}",
        )

    body = resp.json()
    try:
        expires_at = datetime.fromtimestamp(
            int(body["expiryTimeMillis"]) / 1000, tz=timezone.utc
        )
    except (KeyError, ValueError, TypeError) as e:
        raise BillingError(
            "billing_unavailable", "Google's response was missing expiryTimeMillis"
        ) from e

    payment_state = body.get("paymentState")
    # paymentState: 0=pending, 1=received, 2=free trial, 3=pending upgrade/
    # downgrade. Never grant Pro on a *pending* payment.
    paid = payment_state in (1, 2) if payment_state is not None else True
    valid = paid and expires_at > datetime.now(timezone.utc)
    return PlayVerification(
        valid=valid,
        expires_at=expires_at,
        auto_renewing=bool(body.get("autoRenewing", False)),
        order_id=body.get("orderId"),
        payment_state=payment_state,
    )


def clear_token_cache() -> None:
    """Test helper: reset the in-process OAuth token cache."""
    _token_cache.clear()
