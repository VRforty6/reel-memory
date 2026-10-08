"""Google Play Billing API.

POST /v1/billing/verify {package_name, product_id, purchase_token}:
verifies the purchase token with the Google Play Developer API (service
account) and grants Pro until the subscription's expiry. Re-verification
refreshes the entitlement — this is how renewals, cancellations and expiry
are handled (the client re-sends the token, e.g. on app start).

Without a configured service account this returns 503 billing_not_configured
— Pro is never granted without a successful Google verification.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.billing import (
    BillingError,
    BillingNotConfiguredError,
    verify_subscription,
)
from app.db import get_db
from app.entitlements import effective_tier
from app.models import User
from app.schemas import BillingVerifyRequest, BillingVerifyResponse

router = APIRouter()


@router.post("/v1/billing/verify", response_model=BillingVerifyResponse)
def verify_purchase(
    req: BillingVerifyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BillingVerifyResponse:
    try:
        result = verify_subscription(req.package_name, req.product_id, req.purchase_token)
    except BillingNotConfiguredError as e:
        raise HTTPException(
            status_code=503,
            detail={"code": "billing_not_configured", "message": str(e)},
        ) from e
    except BillingError as e:
        status = {
            "invalid_purchase": 400,
            "billing_misconfigured": 503,
            "billing_unavailable": 502,
        }[e.code]
        raise HTTPException(
            status_code=status, detail={"code": e.code, "message": str(e)}
        ) from e

    now = datetime.now(timezone.utc)
    if result.valid and result.expires_at and result.expires_at > now:
        user.tier = "pro"
        # Only ever extend, never shorten, the entitlement on re-verification
        # (a renewal's new expiry is later; clock skew can't steal Pro).
        if user.pro_expires_at is None or result.expires_at > user.pro_expires_at:
            user.pro_expires_at = result.expires_at
        db.commit()
    elif user.tier == "pro" and (
        user.pro_expires_at is None or user.pro_expires_at <= now
    ):
        # Google says the purchase is no longer valid and our record expired:
        # drop back to free. (If Google says invalid but our stored expiry is
        # still in the future, keep it — e.g. a grace-period subscription.)
        user.tier = "free"
        db.commit()

    return BillingVerifyResponse(
        tier=effective_tier(user, now),
        pro_expires_at=user.pro_expires_at,
        auto_renewing=result.auto_renewing,
        order_id=result.order_id,
    )
