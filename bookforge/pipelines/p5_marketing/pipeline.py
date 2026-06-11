"""Pipeline 5 — Marketing (ON/OFF por flags).

- ContentRepurposer: extrae "content atoms" del manuscrito UNA vez;
  cada canal los transforma a su formato.
- OrganicWorker: genera y encola posts por canal activo. La publicacion
  real sale por webhook a n8n (un workflow por canal), manteniendo las
  credenciales sociales fuera de este codigo.
- PaidAdsManager: gestion de Amazon Ads con circuit breakers. La llamada
  real a la Ads API se inyecta como cliente (interfaz AdsClient) para
  poder testear la logica de breakers sin tocar dinero.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from bookforge.core.config import settings
from bookforge.core.llm import LLMClient
from bookforge.core.models import Manuscript, MarketBrief, MarketingFlags
from bookforge.orchestrator.flags import FlagStore, get_flag_store


# ---------------------------------------------------------------------------
# Content atoms
# ---------------------------------------------------------------------------

class ContentAtom(BaseModel):
    kind: str          # insight | quote | list | stat | question
    text: str
    chapter_ref: int


class AtomList(BaseModel):
    atoms: list[ContentAtom] = Field(min_length=10)


class ContentRepurposer:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def extract_atoms(self, ms: Manuscript,
                            book_id: str | None = None) -> list[ContentAtom]:
        sample = "\n\n".join(
            f"[{c.number}] {c.title}\n{c.content_md[:2000]}"
            for c in ms.chapters
        )
        prompt = (
            "Extrae 30-50 'content atoms' de este libro para marketing en "
            "redes (ingles): insights accionables, quotes potentes, listas "
            "cortas, estadisticas, preguntas que enganchan. Cada atom debe "
            "funcionar solo, sin contexto del libro.\n\n" + sample
        )
        result = await self.llm.complete_structured(
            prompt, AtomList, max_tokens=8000, book_id=book_id,
            phase="p5.repurposer",
        )
        return result.atoms

    async def to_channel(self, atom: ContentAtom, channel: str,
                         book_title: str, book_url: str,
                         book_id: str | None = None) -> str:
        formats = {
            "pinterest": "Texto de pin: hook de 1 linea + 2-3 frases + CTA "
                         "al libro. Incluye sugerencia de overlay text.",
            "twitter": "Hilo de 3-5 tweets. Primer tweet = hook.",
            "instagram": "Caption de 80-120 palabras + 5 hashtags de nicho.",
            "tiktok": "Guion de 25-35s: hook (2s), 3 puntos, CTA.",
            "email": "Email corto (120 palabras) para la lista: valor "
                     "primero, mencion del libro al final.",
        }
        prompt = (
            f"Convierte este content atom al formato de {channel} en ingles.\n"
            f"Formato: {formats.get(channel, 'post corto')}\n"
            f"Libro: {book_title} | URL: {book_url}\n"
            f"ATOM: {atom.text}"
        )
        return (await self.llm.complete(
            prompt, max_tokens=1000, temperature=0.8, book_id=book_id,
            phase=f"p5.organic.{channel}",
        )).strip()


# ---------------------------------------------------------------------------
# Organic worker (consulta flags antes de CADA accion)
# ---------------------------------------------------------------------------

class OrganicWorker:
    def __init__(self, llm: LLMClient, flag_store: FlagStore | None = None,
                 n8n_webhook_base: str = ""):
        self.repurposer = ContentRepurposer(llm)
        self.flags = flag_store or get_flag_store()
        self.n8n_base = n8n_webhook_base.rstrip("/")
        self._posted_today: dict[str, int] = {}
        self._today = date.today()

    def _reset_if_new_day(self) -> None:
        if date.today() != self._today:
            self._today = date.today()
            self._posted_today.clear()

    async def dispatch(self, channel: str, content: str,
                       book_title: str) -> dict:
        flags = self.flags.get()
        self._reset_if_new_day()
        if not flags.channel_active(channel):
            return {"status": "skipped", "reason": f"flag OFF: {channel}"}
        if self._posted_today.get(channel, 0) >= flags.organic.posts_per_day_max:
            return {"status": "skipped", "reason": "limite diario alcanzado"}
        if not self.n8n_base:
            return {"status": "queued_local", "channel": channel,
                    "content": content}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.n8n_base}/webhook/bookforge-{channel}",
                json={"content": content, "book": book_title},
            )
        self._posted_today[channel] = self._posted_today.get(channel, 0) + 1
        return {"status": "sent", "channel": channel,
                "n8n_status": resp.status_code}


# ---------------------------------------------------------------------------
# Paid ads con circuit breakers
# ---------------------------------------------------------------------------

class CampaignMetrics(BaseModel):
    campaign_id: str
    spend_usd: Decimal
    sales_usd: Decimal
    days_active: int

    @property
    def acos_pct(self) -> float:
        if self.sales_usd == 0:
            return 999.0
        return float(self.spend_usd / self.sales_usd * 100)


class AdsClient(Protocol):
    """Interfaz a la Amazon Ads API (o mock en tests)."""
    async def get_metrics(self, campaign_id: str) -> CampaignMetrics: ...
    async def pause_campaign(self, campaign_id: str) -> None: ...
    async def total_spend_today(self) -> Decimal: ...


@dataclass
class BreakerEvent:
    campaign_id: str
    rule: str
    detail: str


@dataclass
class PaidAdsManager:
    ads: AdsClient
    flag_store: FlagStore = field(default_factory=get_flag_store)

    async def tick(self, campaign_ids: list[str]) -> list[BreakerEvent]:
        """Una pasada de control. Ejecutar cada N horas via scheduler."""
        flags: MarketingFlags = self.flag_store.get()
        events: list[BreakerEvent] = []

        if not (flags.master and flags.paid.enabled):
            # Pago apagado: pausar todo lo activo (fail-safe)
            for cid in campaign_ids:
                await self.ads.pause_campaign(cid)
                events.append(BreakerEvent(cid, "flags_off",
                                           "marketing pago desactivado"))
            return events

        # Breaker 1: presupuesto diario global
        spend_today = await self.ads.total_spend_today()
        if spend_today > flags.paid.daily_budget_usd:
            for cid in campaign_ids:
                await self.ads.pause_campaign(cid)
                events.append(BreakerEvent(
                    cid, "daily_budget",
                    f"gasto {spend_today} > limite {flags.paid.daily_budget_usd}",
                ))
            return events

        # Breaker 2: ACOS por campaña / Breaker 3: sin ventas en 14 dias
        for cid in campaign_ids:
            m = await self.ads.get_metrics(cid)
            if (m.acos_pct > flags.paid.max_acos_pct
                    and flags.paid.auto_pause_on_breach):
                await self.ads.pause_campaign(cid)
                events.append(BreakerEvent(
                    cid, "max_acos",
                    f"ACOS {m.acos_pct:.1f}% > {flags.paid.max_acos_pct}%",
                ))
            elif m.sales_usd == 0 and m.days_active >= 14:
                await self.ads.pause_campaign(cid)
                events.append(BreakerEvent(
                    cid, "no_sales_14d",
                    "14 dias sin ventas: revisar portada/precio/blurb",
                ))
        return events
