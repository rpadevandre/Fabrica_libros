"""Pipeline 2 — Produccion (Hierarchical Swarm).

Architect (Opus) -> Chapter Writers en paralelo (Sonnet, con semaforo de
concurrencia) -> Continuity Editor -> Line Editor -> QA Scorer (Opus).

Cada writer recibe el Book Bible + outline propio + resumenes de capitulos
adyacentes, no el manuscrito completo (control de contexto y costo).
"""
from __future__ import annotations

import asyncio
import re

from pydantic import BaseModel, Field

from bookforge.core.config import settings
from bookforge.core.llm import LLMClient
from bookforge.core.models import (
    BookOutline, Chapter, Manuscript, MarketBrief, QAScore,
)

# Frases-delatoras de IA. El Line Editor las elimina; el QA penaliza.
AI_TELL_BLACKLIST = [
    "delve", "in today's fast-paced world", "it's important to note",
    "in conclusion,", "furthermore,", "navigating the", "unlock the",
    "in the realm of", "tapestry", "embark on", "game-changer",
    "at the end of the day", "harness the power", "elevate your",
    "dive deep", "treasure trove", "whether you're a",
]

WRITER_CONCURRENCY = 4  # capitulos simultaneos


class ChapterSummary(BaseModel):
    number: int
    summary: str


class ProductionPipeline:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    # ------------------------------------------------------------------
    # Architect
    # ------------------------------------------------------------------
    async def build_outline(self, brief: MarketBrief,
                            book_id: str | None = None) -> BookOutline:
        target_words = {
            "nonfiction_howto": 30000, "guide": 25000, "workbook": 15000,
            "low_content": 2000, "fiction_novella": 25000,
        }.get(brief.book_type, 25000)
        prompt = (
            "Diseña el outline completo de un libro en INGLES a partir de "
            "este MarketBrief. El libro debe atacar explicitamente los "
            "competitor_gaps y cumplir la differentiation.\n\n"
            f"BRIEF: {brief.model_dump_json()}\n\n"
            f"Total de palabras objetivo: ~{target_words}. Reparte "
            "target_words entre capitulos de forma realista.\n"
            "El Book Bible debe incluir banned_phrases con al menos estas: "
            f"{AI_TELL_BLACKLIST[:8]}.\n"
            "Title y subtitle en ingles, optimizados a la keyword principal "
            "sin keyword stuffing."
        )
        return await self.llm.complete_structured(
            prompt, BookOutline, model=settings.model_heavy,
            max_tokens=8000, book_id=book_id, phase="p2.architect",
        )

    # ------------------------------------------------------------------
    # Writers (paralelo con semaforo)
    # ------------------------------------------------------------------
    async def write_chapter(self, outline: BookOutline, index: int,
                            book_id: str | None = None) -> Chapter:
        ch = outline.chapters[index]
        prev_ctx = (
            f"Capitulo anterior ({outline.chapters[index - 1].title}): "
            f"{outline.chapters[index - 1].thesis}" if index > 0 else "Es el primer capitulo."
        )
        next_ctx = (
            f"Capitulo siguiente ({outline.chapters[index + 1].title}): "
            f"{outline.chapters[index + 1].thesis}"
            if index < len(outline.chapters) - 1 else "Es el ultimo capitulo."
        )
        system = (
            "Eres un escritor profesional de nonfiction en ingles. Escribes "
            "con voz humana: anecdotas concretas, frases de longitud "
            "variable, segunda persona cuando aplica, cero relleno. "
            f"PROHIBIDO usar estas frases: {outline.bible.banned_phrases}. "
            f"Tono: {outline.bible.tone}. "
            f"Reglas de estilo: {outline.bible.style_rules}."
        )
        prompt = (
            f"Escribe el capitulo {ch.number}: '{ch.title}' del libro "
            f"'{outline.title}'.\n"
            f"Promesa del libro al lector: {outline.bible.promise_to_reader}\n"
            f"Tesis del capitulo: {ch.thesis}\n"
            f"Puntos clave a cubrir: {ch.key_points}\n"
            f"Contexto: {prev_ctx} | {next_ctx}\n"
            f"Longitud objetivo: {ch.target_words} palabras (+-15%).\n"
            f"Formato: Markdown con subtitulos ##. NO repitas el titulo del "
            f"capitulo como heading inicial, empieza directo con el contenido."
        )
        content = await self.llm.complete(
            prompt, system=system, model=settings.model_volume,
            max_tokens=8000, temperature=0.8, book_id=book_id,
            phase=f"p2.writer.ch{ch.number}",
        )
        return Chapter(number=ch.number, title=ch.title,
                       content_md=content.strip())

    async def draft_manuscript(self, outline: BookOutline,
                               book_id: str | None = None) -> Manuscript:
        sem = asyncio.Semaphore(WRITER_CONCURRENCY)

        async def _write(i: int) -> Chapter:
            async with sem:
                return await self.write_chapter(outline, i, book_id)

        chapters = await asyncio.gather(
            *(_write(i) for i in range(len(outline.chapters)))
        )
        chapters = sorted(chapters, key=lambda c: c.number)
        return Manuscript(title=outline.title, subtitle=outline.subtitle,
                          chapters=list(chapters))

    # ------------------------------------------------------------------
    # Editores (secuenciales, por capitulo para controlar contexto)
    # ------------------------------------------------------------------
    async def _summarize_chapters(self, ms: Manuscript,
                                  book_id: str | None) -> list[ChapterSummary]:
        class _List(BaseModel):
            items: list[ChapterSummary]

        prompt = (
            "Resume cada capitulo en 2 frases (puntos cubiertos, ejemplos "
            "usados). Devuelve un item por capitulo.\n\n"
            + "\n\n".join(
                f"[{c.number}] {c.title}\n{c.content_md[:1500]}"
                for c in ms.chapters
            )
        )
        result = await self.llm.complete_structured(
            prompt, _List, model=settings.model_volume, max_tokens=4000,
            book_id=book_id, phase="p2.continuity.summaries",
        )
        return result.items

    async def continuity_pass(self, ms: Manuscript, outline: BookOutline,
                              book_id: str | None = None) -> Manuscript:
        summaries = await self._summarize_chapters(ms, book_id)
        summary_block = "\n".join(f"[{s.number}] {s.summary}" for s in summaries)
        edited: list[Chapter] = []
        for ch in ms.chapters:
            prompt = (
                "Edita este capitulo para continuidad con el resto del "
                "libro: elimina contenido que repite lo de otros capitulos "
                "(segun los resumenes), unifica terminologia "
                f"({outline.bible.terminology}) y asegura transiciones. "
                "Devuelve SOLO el capitulo editado en Markdown, sin "
                "comentarios.\n\n"
                f"RESUMENES DEL LIBRO:\n{summary_block}\n\n"
                f"CAPITULO [{ch.number}] {ch.title}:\n{ch.content_md}"
            )
            content = await self.llm.complete(
                prompt, model=settings.model_volume, max_tokens=8000,
                temperature=0.3, book_id=book_id,
                phase=f"p2.continuity.ch{ch.number}",
            )
            edited.append(Chapter(number=ch.number, title=ch.title,
                                  content_md=content.strip()))
        return Manuscript(title=ms.title, subtitle=ms.subtitle,
                          chapters=edited, revision=ms.revision + 1)

    async def line_edit_pass(self, ms: Manuscript, outline: BookOutline,
                             book_id: str | None = None) -> Manuscript:
        banned = sorted(set(
            p.lower() for p in (AI_TELL_BLACKLIST + outline.bible.banned_phrases)
        ))
        edited: list[Chapter] = []
        for ch in ms.chapters:
            prompt = (
                "Haz line editing de este capitulo: voz activa, ritmo "
                "variado, elimina muletillas de IA y cualquier frase de "
                f"esta lista prohibida: {banned}. Manten el contenido y la "
                "longitud (+-10%). Devuelve SOLO el Markdown editado.\n\n"
                f"{ch.content_md}"
            )
            content = await self.llm.complete(
                prompt, model=settings.model_volume, max_tokens=8000,
                temperature=0.3, book_id=book_id,
                phase=f"p2.line_edit.ch{ch.number}",
            )
            edited.append(Chapter(number=ch.number, title=ch.title,
                                  content_md=content.strip()))
        return Manuscript(title=ms.title, subtitle=ms.subtitle,
                          chapters=edited, revision=ms.revision + 1)

    # ------------------------------------------------------------------
    # QA
    # ------------------------------------------------------------------
    @staticmethod
    def detect_ai_tells(ms: Manuscript) -> dict[str, int]:
        """Deteccion determinista de frases prohibidas (rapida y gratis)."""
        text = ms.to_markdown().lower()
        hits: dict[str, int] = {}
        for phrase in AI_TELL_BLACKLIST:
            count = len(re.findall(re.escape(phrase.lower()), text))
            if count:
                hits[phrase] = count
        return hits

    async def qa_score(self, ms: Manuscript, brief: MarketBrief,
                       book_id: str | None = None) -> QAScore:
        tells = self.detect_ai_tells(ms)
        sample = "\n\n".join(
            f"[{c.number}] {c.title}\n{c.content_md[:2500]}"
            for c in ms.chapters
        )
        prompt = (
            "Evalua este manuscrito con la rubrica del schema (structure/20, "
            "depth_value/25, prose_quality/20, originality/20, "
            "brief_compliance/15). Se duro: 80+ significa publicable tal "
            "cual. En feedback da acciones concretas por capitulo.\n\n"
            f"BRIEF (gaps que el libro promete resolver): "
            f"{brief.competitor_gaps} | diferenciacion: {brief.differentiation}\n"
            f"AI-tells detectados deterministicamente: {tells}\n"
            f"Total de palabras: {ms.total_words}\n\n"
            f"MANUSCRITO (muestras por capitulo):\n{sample}"
        )
        score = await self.llm.complete_structured(
            prompt, QAScore, model=settings.model_heavy, max_tokens=4000,
            book_id=book_id, phase="p2.qa_scorer",
        )
        # Penalizacion determinista adicional por AI-tells
        if tells:
            penalty = min(10.0, float(sum(tells.values())) * 0.5)
            score.originality = max(0.0, score.originality - penalty)
            score.feedback.append(
                f"Penalizacion automatica por AI-tells: -{penalty} "
                f"(frases: {list(tells)})"
            )
        return score

    # ------------------------------------------------------------------
    # Orquestacion completa del pipeline
    # ------------------------------------------------------------------
    async def run(self, brief: MarketBrief,
                  book_id: str | None = None) -> tuple[BookOutline, Manuscript, QAScore]:
        outline = await self.build_outline(brief, book_id)
        ms = await self.draft_manuscript(outline, book_id)
        ms = await self.continuity_pass(ms, outline, book_id)
        ms = await self.line_edit_pass(ms, outline, book_id)
        score = await self.qa_score(ms, brief, book_id)

        cycles = 0
        while not score.passes(settings.qa_threshold) and cycles < settings.max_qa_cycles:
            cycles += 1
            ms = await self._revise_with_feedback(ms, score, book_id)
            score = await self.qa_score(ms, brief, book_id)
        return outline, ms, score

    async def _revise_with_feedback(self, ms: Manuscript, score: QAScore,
                                    book_id: str | None) -> Manuscript:
        edited: list[Chapter] = []
        feedback = "\n".join(f"- {f}" for f in score.feedback)
        for ch in ms.chapters:
            prompt = (
                "Revisa este capitulo aplicando el feedback de QA que le "
                "aplique. Devuelve SOLO el Markdown revisado.\n\n"
                f"FEEDBACK GLOBAL:\n{feedback}\n\n"
                f"CAPITULO [{ch.number}] {ch.title}:\n{ch.content_md}"
            )
            content = await self.llm.complete(
                prompt, model=settings.model_volume, max_tokens=8000,
                temperature=0.4, book_id=book_id,
                phase=f"p2.revision.ch{ch.number}",
            )
            edited.append(Chapter(number=ch.number, title=ch.title,
                                  content_md=content.strip()))
        return Manuscript(title=ms.title, subtitle=ms.subtitle,
                          chapters=edited, revision=ms.revision + 1)
