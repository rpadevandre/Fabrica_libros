"""Persistencia: catalogo de libros, briefs y registro de costos.

SQLite por defecto (cero infraestructura), Postgres via BF_DATABASE_URL.
Los documentos grandes (brief, outline, manuscrito) se guardan como JSON
del modelo Pydantic: el schema relacional solo indexa lo consultable.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON, DateTime, Integer, Numeric, String, Text, create_engine, select,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, Session, mapped_column, sessionmaker,
)

from bookforge.core.config import settings
from bookforge.core.models import Book, BookState


class Base(DeclarativeBase):
    pass


class BookRow(Base):
    __tablename__ = "books"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    line_slug: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(300))
    state: Mapped[str] = mapped_column(String(32), index=True)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    payload: Mapped[dict] = mapped_column(JSON)  # Book serializado completo
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class CostRow(Base):
    __tablename__ = "costs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                    autoincrement=True)
    book_id: Mapped[Optional[str]] = mapped_column(String(36), index=True,
                                                   nullable=True)
    phase: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                    autoincrement=True)
    book_id: Mapped[str] = mapped_column(String(36), index=True)
    event: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


_engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(_engine)


class BookRepository:
    def __init__(self, session_factory=SessionLocal):
        self._sf = session_factory

    def save(self, book: Book) -> None:
        with self._sf() as s:
            row = s.get(BookRow, str(book.id))
            payload = book.model_dump(mode="json")
            if row is None:
                row = BookRow(id=str(book.id), line_slug=book.line_slug,
                              title=book.title, state=book.state.value,
                              cost_usd=book.cost_usd, payload=payload)
                s.add(row)
            else:
                row.title = book.title
                row.state = book.state.value
                row.cost_usd = book.cost_usd
                row.payload = payload
            s.commit()

    def get(self, book_id: uuid.UUID | str) -> Optional[Book]:
        with self._sf() as s:
            row = s.get(BookRow, str(book_id))
            return Book.model_validate(row.payload) if row else None

    def list_by_state(self, state: BookState | None = None) -> list[Book]:
        with self._sf() as s:
            stmt = select(BookRow).order_by(BookRow.updated_at.desc())
            if state:
                stmt = stmt.where(BookRow.state == state.value)
            return [Book.model_validate(r.payload)
                    for r in s.scalars(stmt).all()]

    def log_event(self, book_id: uuid.UUID | str, event: str,
                  detail: str = "") -> None:
        with self._sf() as s:
            s.add(EventRow(book_id=str(book_id), event=event, detail=detail))
            s.commit()


class CostTracker:
    """Implementa CostTrackerProtocol. Cada llamada LLM/imagen queda aqui."""

    def __init__(self, session_factory=SessionLocal):
        self._sf = session_factory

    def record(self, *, book_id: str | None, phase: str, model: str,
               cost_usd: Decimal, input_tokens: int = 0,
               output_tokens: int = 0) -> None:
        with self._sf() as s:
            s.add(CostRow(book_id=book_id, phase=phase, model=model,
                          cost_usd=cost_usd, input_tokens=input_tokens,
                          output_tokens=output_tokens))
            # acumular en el libro
            if book_id:
                row = s.get(BookRow, book_id)
                if row:
                    row.cost_usd = (row.cost_usd or Decimal("0")) + cost_usd
                    payload = dict(row.payload)
                    payload["cost_usd"] = str(row.cost_usd)
                    row.payload = payload
            s.commit()

    def total_for_book(self, book_id: str) -> Decimal:
        with self._sf() as s:
            rows = s.scalars(
                select(CostRow).where(CostRow.book_id == book_id)
            ).all()
            return sum((r.cost_usd for r in rows), Decimal("0"))

    def total_current_month(self) -> Decimal:
        """Gasto LLM/imagen del mes calendario actual (freno del daemon)."""
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0,
                                  microsecond=0)
        with self._sf() as s:
            rows = s.scalars(
                select(CostRow).where(CostRow.created_at >= month_start)
            ).all()
            return sum((r.cost_usd for r in rows), Decimal("0"))
