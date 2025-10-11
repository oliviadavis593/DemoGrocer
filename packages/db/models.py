"""SQLAlchemy models and helpers for compliance data."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .core import ensure_db_path

_ENGINE_CACHE: dict[Path, Engine] = {}
_SESSION_FACTORY_CACHE: dict[Path, sessionmaker[Session]] = {}


class Base(DeclarativeBase):
    """Declarative base for FoodFlow ORM models."""


def _resolve_db_path(path: Path | None = None) -> Path:
    return ensure_db_path(path).resolve()


def get_engine(path: Path | None = None) -> Engine:
    """Return or create a cached SQLAlchemy engine for the configured database."""

    db_path = _resolve_db_path(path)
    engine = _ENGINE_CACHE.get(db_path)
    if engine is None:
        engine = create_engine(
            f"sqlite:///{db_path}",
            future=True,
            echo=False,
        )
        _ENGINE_CACHE[db_path] = engine
    return engine


def get_session_factory(path: Path | None = None) -> sessionmaker[Session]:
    """Return a cached session factory bound to the configured engine."""

    db_path = _resolve_db_path(path)
    factory = _SESSION_FACTORY_CACHE.get(db_path)
    if factory is None:
        engine = get_engine(db_path)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
        _SESSION_FACTORY_CACHE[db_path] = factory
    return factory


@contextmanager
def compliance_session(path: Path | None = None) -> Iterator[Session]:
    """Context manager yielding a SQLAlchemy session committed on success."""

    factory = get_session_factory(path)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class ComplianceEvent(Base):
    """SQLAlchemy model mirroring the compliance schema."""

    __tablename__ = "compliance_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    product_code: Mapped[str] = mapped_column(String(128), nullable=False)
    product_name: Mapped[str] = mapped_column(String(256), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    lot_code: Mapped[Optional[str]] = mapped_column(String(128))
    life_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    store: Mapped[str] = mapped_column(String(128), nullable=False)
    location_id: Mapped[Optional[int]] = mapped_column(Integer)

    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(64))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    quantity_units: Mapped[float] = mapped_column(Float, nullable=False)
    uom: Mapped[Optional[str]] = mapped_column(String(32))
    weight_lbs: Mapped[Optional[float]] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float, nullable=False)
    fair_market_value: Mapped[float] = mapped_column(Float, nullable=False)
    extended_value: Mapped[Optional[float]] = mapped_column(Float)

    captured_by: Mapped[str] = mapped_column(String(128), nullable=False)
    staff_id: Mapped[Optional[str]] = mapped_column(String(64))
    photo_url: Mapped[Optional[str]] = mapped_column(String(512))

    irs_qualified_org: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    irs_charitable_purpose: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    irs_wholesome_food: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    irs_no_compensation: Mapped[Optional[bool]] = mapped_column(Boolean, default=True)
    irs_proper_handling: Mapped[Optional[bool]] = mapped_column(Boolean, default=True)

    meta_json: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


def create_all(path: Path | None = None) -> None:
    """Ensure all ORM tables are created."""

    engine = get_engine(path)
    Base.metadata.create_all(engine)


__all__ = [
    "ComplianceEvent",
    "Base",
    "get_engine",
    "get_session_factory",
    "compliance_session",
    "create_all",
]
