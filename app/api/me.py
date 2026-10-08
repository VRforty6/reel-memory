"""GET /v1/me — profile + effective tier + quota usage."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.entitlements import usage_summary
from app.models import User
from app.schemas import BucketUsage, MeResponse

router = APIRouter()


@router.get("/v1/me", response_model=MeResponse)
def get_me(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MeResponse:
    summary = usage_summary(db, user)
    return MeResponse(
        id=user.id,
        email=user.email,
        tier=summary["tier"],
        pro_expires_at=user.pro_expires_at,
        google_linked=user.google_sub is not None,
        has_password=user.password_hash is not None,
        usage={
            bucket: BucketUsage(**info) for bucket, info in summary["usage"].items()
        },
    )


__all__ = ["router"]
