"""Freemium entitlements: tiers, quota limits, and usage accounting.

Buckets:
- "captures":      every new capture (URL share, file upload, web page) — monthly
- "questions":     ask + actions + brief (each burns one chat call chain) — daily
- "verifications": ask(verify=true) + brief validation — daily

Pro is `settings.pro_quota_multiplier` x the free limits (abuse caps, not
truly infinite). Quota exhaustion returns HTTP 402 with a machine-readable
body the client turns into a paywall.

Pure helpers (period math, limit lookup) are unit-testable; the DB counter
functions take a session.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models import UsageCounter, User

BUCKET_CAPTURES = "captures"
BUCKET_QUESTIONS = "questions"
BUCKET_VERIFICATIONS = "verifications"

_MONTHLY_BUCKETS = {BUCKET_CAPTURES}
_DAILY_BUCKETS = {BUCKET_QUESTIONS, BUCKET_VERIFICATIONS}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def effective_tier(user: User, now: datetime | None = None) -> str:
    """'pro' only when the user record says pro AND the subscription hasn't
    expired. An expired subscription reads as free (lazy downgrade — no
    background job needed; re-verification via /v1/billing/verify refreshes)."""
    now = now or _utcnow()
    if user.tier == "pro" and (user.pro_expires_at is None or user.pro_expires_at > now):
        return "pro"
    return "free"


def limits_for(tier: str) -> dict[str, int]:
    """Quota limits per bucket for a tier. Pure (unit-testable)."""
    mult = settings.pro_quota_multiplier if tier == "pro" else 1
    return {
        BUCKET_CAPTURES: settings.free_captures_monthly * mult,
        BUCKET_QUESTIONS: settings.free_questions_daily * mult,
        BUCKET_VERIFICATIONS: settings.free_verifications_daily * mult,
    }


def period_for(bucket: str, now: datetime) -> str:
    """UTC calendar period key for a bucket. Pure (unit-testable)."""
    if bucket in _MONTHLY_BUCKETS:
        return now.strftime("%Y-%m")
    return now.strftime("%Y-%m-%d")


def resets_at_for(bucket: str, now: datetime) -> datetime:
    """Start of the next UTC period — when this bucket's quota resets."""
    if bucket in _MONTHLY_BUCKETS:
        days = monthrange(now.year, now.month)[1]
        nxt = (now.replace(day=1) + timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt


def quota_exceeded_error(
    *, tier: str, bucket: str, limit: int, used: int, resets_at: datetime
) -> HTTPException:
    return HTTPException(
        status_code=402,
        detail={
            "code": "quota_exceeded",
            "message": (
                f"you've used this month's/day's {bucket} allowance "
                f"({used}/{limit}) — upgrade to Pro for more"
                if tier == "free"
                else f"{bucket} rate limit reached — try again later"
            ),
            "tier": tier,
            "bucket": bucket,
            "limit": limit,
            "used": used,
            "resets_at": resets_at.isoformat(),
        },
    )


def check_and_increment_quota(
    db: Session, user: User, bucket: str, now: datetime | None = None
) -> int:
    """Consume one unit of `bucket` quota. Returns the new usage count.

    Raises HTTPException 402 when the quota is exhausted. Uses
    SELECT ... FOR UPDATE so concurrent requests can't both slip under
    the limit.
    """
    now = now or _utcnow()
    tier = effective_tier(user, now)
    limit = limits_for(tier)[bucket]
    period = period_for(bucket, now)

    row = (
        db.query(UsageCounter)
        .filter(
            UsageCounter.user_id == user.id,
            UsageCounter.bucket == bucket,
            UsageCounter.period == period,
        )
        .with_for_update()
        .first()
    )
    if row is None:
        row = UsageCounter(user_id=user.id, bucket=bucket, period=period, count=0)
        db.add(row)
        db.flush()
    if row.count >= limit:
        db.rollback()
        raise quota_exceeded_error(
            tier=tier, bucket=bucket, limit=limit, used=row.count,
            resets_at=resets_at_for(bucket, now),
        )
    row.count += 1
    db.commit()
    return row.count


def usage_summary(db: Session, user: User, now: datetime | None = None) -> dict:
    """Per-bucket {used, limit, resets_at} for GET /v1/me."""
    now = now or _utcnow()
    tier = effective_tier(user, now)
    limits = limits_for(tier)
    summary = {}
    for bucket in (BUCKET_CAPTURES, BUCKET_QUESTIONS, BUCKET_VERIFICATIONS):
        period = period_for(bucket, now)
        row = (
            db.query(UsageCounter)
            .filter(
                UsageCounter.user_id == user.id,
                UsageCounter.bucket == bucket,
                UsageCounter.period == period,
            )
            .first()
        )
        summary[bucket] = {
            "used": row.count if row else 0,
            "limit": limits[bucket],
            "resets_at": resets_at_for(bucket, now).isoformat(),
        }
    return {"tier": tier, "usage": summary}
