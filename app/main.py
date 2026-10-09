"""Reel Memory API — FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import auth, billing, captures, categories, me, memories, search, updates
from app.config import settings
from app.db import SessionLocal, get_or_create_local_user, init_db

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev convenience: ensure extension + tables exist. Production deploys
    # should apply migrations/001_initial.sql..004_auth_billing.sql instead (see README).
    init_db()
    if settings.dev_seed_local_user:
        # Throwaway local dev only: seed the legacy single user so the API is
        # usable without signup. Never enable in production.
        db = SessionLocal()
        try:
            get_or_create_local_user(db)
        finally:
            db.close()
        log.warning("DEV_SEED_LOCAL_USER is on — single-user mode, do not use in production")
    if not settings.jwt_secret:
        log.warning(
            "JWT_SECRET is not set — /v1/auth/* endpoints will fail until it is "
            "(see .env.example)"
        )
    yield


app = FastAPI(title="Reel Memory", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(me.router)
app.include_router(billing.router)
app.include_router(captures.router)
app.include_router(categories.router)
app.include_router(memories.router)
app.include_router(search.router)
app.include_router(updates.router)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": "0.1.0"}


def run_worker() -> None:
    """Entrypoint for the background processing worker: python -m app.main worker."""
    from app.pipeline.providers import build_providers
    from app.pipeline.worker import Worker

    # Fail fast on misconfigured providers (bad name / missing key) instead of
    # discovering it mid-pipeline; "none" defaults keep the loud stubs.
    Worker(SessionLocal, build_providers()).run_forever()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        run_worker()
    else:
        import uvicorn

        uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
