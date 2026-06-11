"""Modo autonomo de BookForge.

Niveles de autonomia (acumulativos):
  0 MANUAL      : nada automatico (comportamiento actual).
  1 AUTO_BRIEF  : auto-aprueba briefs GO con score >= brief_auto_threshold
                  y lanza produccion. Gate 2 sigue siendo humano.
  2 AUTO_BOOK   : ademas auto-aprueba manuscritos con QA >=
                  manuscript_auto_threshold y avanza visual+formatting.
                  La publicacion REAL sigue respetando kdp_dry_run.

Frenos duros (siempre activos, en cualquier nivel):
  - monthly_budget_usd : si el gasto LLM/imagen del mes lo supera, el
    daemon deja de iniciar trabajo nuevo (lo en curso termina).
  - max_books_in_flight: limita libros activos simultaneos.
  - max_new_books_per_month: limita creacion de libros.
  - kdp_dry_run        : el daemon NUNCA lo anula.

El daemon es un loop asyncio cuyo `tick()` es puro respecto a sus
dependencias inyectadas: se testea completo con fakes.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

from bookforge.core.config import settings
from bookforge.core.models import Book, BookState, MarketBrief

logger = logging.getLogger("bookforge.daemon")

IN_FLIGHT_STATES = {
    BookState.BRIEF_APPROVED, BookState.OUTLINE, BookState.DRAFTING,
    BookState.EDITING, BookState.QA_SCORING, BookState.QA_FAILED,
    BookState.HUMAN_REVIEW, BookState.VISUAL_PRODUCTION,
    BookState.FORMATTING, BookState.READY_TO_PUBLISH, BookState.PUBLISHING,
}


class AutonomyConfig(BaseModel):
    level: int = Field(default=0, ge=0, le=2)
    seed_interests: list[str] = Field(default_factory=list)
    line_slug: str = "ia-press"
    brief_auto_threshold: float = 85.0
    manuscript_auto_threshold: float = 90.0
    monthly_budget_usd: Decimal = Decimal("100.00")
    max_books_in_flight: int = 2
    max_new_books_per_month: int = 4
    scout_interval_hours: float = 168.0      # semanal
    tick_interval_seconds: float = 300.0     # 5 min
    notify_webhook: str = ""                  # n8n -> email/telegram

    @classmethod
    def load(cls, path: Path | None = None) -> "AutonomyConfig":
        path = path or settings.data_dir / "autonomy.json"
        if path.exists():
            try:
                return cls.model_validate_json(path.read_text("utf-8"))
            except Exception:
                logger.warning("autonomy.json corrupto: usando nivel 0")
        return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or settings.data_dir / "autonomy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), "utf-8")


# ---------------------------------------------------------------------------
# Politicas puras (testeables sin IO)
# ---------------------------------------------------------------------------

def should_auto_approve_brief(brief: MarketBrief, cfg: AutonomyConfig) -> bool:
    return (cfg.level >= 1
            and brief.verdict == "GO"
            and brief.viability_score >= cfg.brief_auto_threshold)


def should_auto_approve_manuscript(book: Book, cfg: AutonomyConfig) -> bool:
    return (cfg.level >= 2
            and book.qa_score is not None
            and book.qa_score.total >= cfg.manuscript_auto_threshold)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class AutonomousDaemon:
    """Dependencias inyectadas para testeo total:

    repo            : BookRepository-like (get/save/list_by_state/log_event)
    monthly_spend   : Callable[[], Decimal]
    scout           : async (interests, line) -> list[Book] creados en RESEARCH
    produce         : async (book_id) -> None  (runner.produce_book parcial)
    postproduce     : async (book_id) -> None  (visual+formatting; opcional)
    """

    def __init__(
        self,
        cfg: AutonomyConfig,
        repo,
        monthly_spend: Callable[[], Decimal],
        scout: Callable[[list[str], str], Awaitable[list[Book]]],
        produce: Callable[[str], Awaitable[None]],
        postproduce: Optional[Callable[[str], Awaitable[None]]] = None,
        notifier: Optional[Callable[[str, dict], Awaitable[None]]] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.cfg = cfg
        self.repo = repo
        self.monthly_spend = monthly_spend
        self.scout = scout
        self.produce = produce
        self.postproduce = postproduce
        self.notifier = notifier or self._default_notifier
        self.clock = clock
        self._last_scout: Optional[datetime] = None
        self._created_this_month = 0
        self._month_key = self._current_month()
        self._running = False

    # -- helpers ----------------------------------------------------------
    def _current_month(self) -> str:
        return self.clock().strftime("%Y-%m")

    def _roll_month(self) -> None:
        key = self._current_month()
        if key != self._month_key:
            self._month_key = key
            self._created_this_month = 0

    async def _default_notifier(self, event: str, payload: dict) -> None:
        logger.info("notify %s %s", event, payload)
        if not self.cfg.notify_webhook:
            return
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(self.cfg.notify_webhook,
                                  json={"event": event, **payload})
        except Exception as exc:
            logger.warning("notificacion fallida: %r", exc)

    def budget_exceeded(self) -> bool:
        return self.monthly_spend() >= self.cfg.monthly_budget_usd

    def in_flight_count(self) -> int:
        return sum(1 for b in self.repo.list_by_state()
                   if b.state in IN_FLIGHT_STATES)

    # -- tick: una pasada completa del cerebro ----------------------------
    async def tick(self) -> dict:
        self._roll_month()
        actions: list[str] = []

        if self.cfg.level == 0:
            return {"actions": ["level_0_idle"]}

        # Freno 1: presupuesto. No inicia trabajo nuevo.
        if self.budget_exceeded():
            await self.notifier("budget_exceeded", {
                "spend": str(self.monthly_spend()),
                "limit": str(self.cfg.monthly_budget_usd),
            })
            return {"actions": ["budget_exceeded_idle"]}

        in_flight = self.in_flight_count()

        # 1) Scouting periodico si hay capacidad
        due = (self._last_scout is None or
               (self.clock() - self._last_scout).total_seconds()
               >= self.cfg.scout_interval_hours * 3600)
        if (due and self.cfg.seed_interests
                and in_flight < self.cfg.max_books_in_flight
                and self._created_this_month < self.cfg.max_new_books_per_month):
            new_books = await self.scout(self.cfg.seed_interests,
                                         self.cfg.line_slug)
            self._last_scout = self.clock()
            self._created_this_month += len(new_books)
            actions.append(f"scouted:{len(new_books)}")
            await self.notifier("scout_done",
                                {"created": [str(b.id) for b in new_books]})

        # 2) Auto-aprobacion de briefs (nivel >= 1)
        for book in self.repo.list_by_state(BookState.RESEARCH):
            if self.in_flight_count() >= self.cfg.max_books_in_flight:
                break
            if book.market_brief and should_auto_approve_brief(
                    book.market_brief, self.cfg):
                book.transition(BookState.BRIEF_APPROVED)
                self.repo.save(book)
                self.repo.log_event(book.id, "brief_auto_approved",
                                    f"score={book.market_brief.viability_score}")
                actions.append(f"brief_approved:{book.id}")
                await self.notifier("brief_auto_approved", {
                    "book_id": str(book.id), "title": book.title,
                    "score": book.market_brief.viability_score,
                })

        # 3) Producir lo aprobado (secuencial: control de costo y carga)
        for book in self.repo.list_by_state(BookState.BRIEF_APPROVED):
            if self.budget_exceeded():
                break
            await self.produce(str(book.id))
            actions.append(f"produced:{book.id}")
            refreshed = self.repo.get(book.id)
            await self.notifier("production_done", {
                "book_id": str(book.id),
                "state": refreshed.state.value if refreshed else "?",
                "qa": (refreshed.qa_score.total
                       if refreshed and refreshed.qa_score else None),
            })

        # 4) Gate 2: auto (nivel 2) o notificar al humano (nivel 1)
        for book in self.repo.list_by_state(BookState.HUMAN_REVIEW):
            if should_auto_approve_manuscript(book, self.cfg):
                book.transition(BookState.VISUAL_PRODUCTION)
                self.repo.save(book)
                self.repo.log_event(book.id, "manuscript_auto_approved",
                                    f"qa={book.qa_score.total}")
                actions.append(f"manuscript_approved:{book.id}")
                if self.postproduce is not None:
                    await self.postproduce(str(book.id))
                    actions.append(f"postproduced:{book.id}")
            else:
                await self.notifier("awaiting_human_review", {
                    "book_id": str(book.id), "title": book.title,
                    "qa": book.qa_score.total if book.qa_score else None,
                })

        return {"actions": actions or ["idle"]}

    # -- loop --------------------------------------------------------------
    async def run_forever(self) -> None:
        self._running = True
        await self.notifier("daemon_started",
                            {"level": self.cfg.level,
                             "budget": str(self.cfg.monthly_budget_usd)})
        while self._running:
            try:
                result = await self.tick()
                if result["actions"] != ["idle"]:
                    logger.info("tick: %s", result["actions"])
            except Exception as exc:
                logger.exception("tick fallo: %r", exc)
                await self.notifier("daemon_error", {"error": repr(exc)})
            await asyncio.sleep(self.cfg.tick_interval_seconds)

    def stop(self) -> None:
        self._running = False
