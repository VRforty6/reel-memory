"""Database engine, sessions, and local-user bootstrap. PRD §34, SEC-005."""

from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base, User

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Dev convenience: create extension + tables. Production deploys use migrations/."""
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=engine)


def get_or_create_local_user(db: Session) -> User:
    """Single-user local mode. Every query elsewhere filters by user_id so per-user
    isolation (SEC-005) can be enforced without schema changes when auth lands."""
    user = db.query(User).filter(User.email == settings.local_user_email).first()
    if user is None:
        user = User(email=settings.local_user_email)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
