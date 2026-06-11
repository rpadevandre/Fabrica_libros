"""Pipeline 1 — Market Intelligence.

Flujo: NicheScout -> (CompetitionAnalyst + KeywordResearcher +
AudienceProfiler en paralelo) -> ViabilityJudge -> MarketBrief.

Las fuentes externas (scraping, Publisher Rocket CSV) entran como texto
de contexto opcional; el pipeline funciona solo con el conocimiento del
modelo + web search del lado del agente cuando este disponible.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import BaseModel, Field

from bookforge.core.config import settings
from bookforge.core.llm import LLMClient
from bookforge.core.models import (
    AudiencePersona, CompetitorSnapshot, MarketBrief,
)


class NicheCandidate(BaseModel):
    niche: str
    book_type: str
    rationale: str
    demand_signals: list[str]


class NicheCandidateList(BaseModel):
    candidates: list[NicheCandidate] = Field(min_length=1)


class CompetitionReport(BaseModel):
    competitors: list[CompetitorSnapshot]
    market_gaps: list[str]
    saturation_note: str


class KeywordReport(BaseModel):
    keywords: list[str] = Field(min_length=1, max_length=7)
    categories: list[str] = Field(min_length=1, max_length=3)
    long_tails: list[str] = Field(default_factory=list)


SYSTEM_BASE = (
    "Eres un analista senior de mercado editorial para Amazon KDP, "
    "especializado en audiencias angloparlantes (US/UK) y europeas. "
    "Tu analisis es honesto: si un nicho es malo, lo dices. "
    "Todo titulo y keyword que propongas debe estar en ingles."
)


class MarketIntelligencePipeline:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def scout_niches(self, seed_interests: list[str], n: int = 10,
                           extra_context: str = "") -> list[NicheCandidate]:
        external_block = f"Datos externos:\n{extra_context}" if extra_context else ""
        prompt = (
            f"Genera {n} candidatos de nicho para libros autopublicados en "
            f"KDP con demanda real y competencia abordable.\n"
            f"Intereses semilla del publisher: {', '.join(seed_interests)}.\n"
            f"Tipos validos: nonfiction_howto, workbook, guide, low_content, "
            f"fiction_novella.\n"
            f"{external_block}"
            f"\nPara cada candidato incluye señales de demanda concretas "
            f"(busquedas, comunidades, tendencias)."
        )
        result = await self.llm.complete_structured(
            prompt, NicheCandidateList, system=SYSTEM_BASE,
            model=settings.model_volume, phase="p1.niche_scout",
        )
        return result.candidates

    async def analyze_competition(self, niche: str,
                                  extra_context: str = "") -> CompetitionReport:
        external_block = f"Datos de scraping/Rocket:\n{extra_context}" if extra_context else ""
        prompt = (
            f"Analiza la competencia en Amazon para el nicho: '{niche}'.\n"
            f"Identifica hasta 10 competidores tipicos del top de la "
            f"categoria, sus debilidades (lo que se quejan los lectores en "
            f"reviews de 3 estrellas) y los gaps de contenido del mercado.\n"
            f"{external_block}"
        )
        return await self.llm.complete_structured(
            prompt, CompetitionReport, system=SYSTEM_BASE,
            model=settings.model_volume, phase="p1.competition",
        )

    async def research_keywords(self, niche: str) -> KeywordReport:
        prompt = (
            f"Para un libro KDP en el nicho '{niche}', genera:\n"
            f"- 7 backend keywords (frases de 2-4 palabras, alto intent de "
            f"compra, baja-media competencia)\n"
            f"- 3 categorias KDP especificas (rutas completas)\n"
            f"- long-tails de autocomplete de Amazon relevantes"
        )
        return await self.llm.complete_structured(
            prompt, KeywordReport, system=SYSTEM_BASE,
            model=settings.model_volume, phase="p1.keywords",
        )

    async def profile_audience(self, niche: str) -> AudiencePersona:
        prompt = (
            f"Construye la persona del lector objetivo para un libro en el "
            f"nicho '{niche}'. Mercados: US, UK y Europa angloparlante. "
            f"Incluye dolores reales, vocabulario que usa, y objeciones de "
            f"compra tipicas."
        )
        return await self.llm.complete_structured(
            prompt, AudiencePersona, system=SYSTEM_BASE,
            model=settings.model_volume, phase="p1.audience",
        )

    async def judge_viability(
        self,
        candidate: NicheCandidate,
        competition: CompetitionReport,
        keywords: KeywordReport,
        persona: AudiencePersona,
    ) -> MarketBrief:
        prompt = (
            "Sintetiza el siguiente analisis y emite un MarketBrief con "
            "veredicto GO/NO_GO. Se exigente: el umbral GO es score >= 70 y "
            "solo si hay diferenciacion real frente a los gaps detectados.\n\n"
            f"NICHO: {candidate.model_dump_json()}\n\n"
            f"COMPETENCIA: {competition.model_dump_json()}\n\n"
            f"KEYWORDS: {keywords.model_dump_json()}\n\n"
            f"PERSONA: {persona.model_dump_json()}\n\n"
            "Define tambien working_title (ingles, optimizado a keyword "
            "principal), estrategia de precio realista y estimacion de "
            "revenue mensual (rango conservador)."
        )
        brief = await self.llm.complete_structured(
            prompt, MarketBrief, system=SYSTEM_BASE,
            model=settings.model_heavy, max_tokens=6000,
            phase="p1.viability_judge",
        )
        # Coherencia dura: el judge no puede decir GO con score bajo
        if brief.viability_score < 70 and brief.verdict == "GO":
            brief.verdict = "NO_GO"
        return brief

    async def run_for_niche(self, candidate: NicheCandidate,
                            extra_context: str = "") -> MarketBrief:
        competition, keywords, persona = await asyncio.gather(
            self.analyze_competition(candidate.niche, extra_context),
            self.research_keywords(candidate.niche),
            self.profile_audience(candidate.niche),
        )
        return await self.judge_viability(candidate, competition,
                                          keywords, persona)

    async def run(self, seed_interests: list[str],
                  top_n_to_brief: int = 3) -> list[MarketBrief]:
        """Pipeline completo: scout -> brief de los N mejores candidatos."""
        candidates = await self.scout_niches(seed_interests)
        briefs: list[MarketBrief] = []
        for cand in candidates[:top_n_to_brief]:
            briefs.append(await self.run_for_niche(cand))
        briefs.sort(key=lambda b: b.viability_score, reverse=True)
        return briefs
