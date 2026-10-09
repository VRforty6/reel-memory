"""Hierarchical category tree API.

The tree is per-user and derived from primary memory assignments.  Reclassify is
keyless/local and preserves any future manual primary assignment by default.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import User
from app.schemas import CategoryReclassifyResponse, CategoryTreeResponse
from app.taxonomy import build_category_tree, reclassify_user_memories

router = APIRouter()


@router.get("/v1/categories/tree", response_model=CategoryTreeResponse)
def get_category_tree(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CategoryTreeResponse:
    tree = build_category_tree(db, user.id)
    return CategoryTreeResponse(
        categories=tree,
        total_memories=sum(node["total_count"] for node in tree),
    )


@router.post("/v1/categories/reclassify", response_model=CategoryReclassifyResponse)
def reclassify_categories(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CategoryReclassifyResponse:
    counts = reclassify_user_memories(db, user.id, preserve_manual=True)
    return CategoryReclassifyResponse(
        classified=sum(counts.values()),
        counts=counts,
    )
