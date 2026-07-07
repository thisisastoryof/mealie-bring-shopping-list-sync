from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils import utcnow


class ItemMap(Base):
    """One row per logical item, mapping a Mealie item to a Bring item.

    See DESIGN.md §4. Identity is tracked by stable IDs; ``norm_key`` is a
    fallback matcher only. ``deleted_at`` acts as a tombstone so a removed item
    cannot be resurrected by a stale copy on the other side.
    """

    __tablename__ = "item_map"

    internal_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mealie_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    bring_uuid: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    norm_key: Mapped[str] = mapped_column(String, nullable=False, index=True, default="")

    mealie_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    bring_hash: Mapped[str | None] = mapped_column(String, nullable=True)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Meta(Base):
    """Small key/value store for cursors, last-poll timestamps, schema version."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
