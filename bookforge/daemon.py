"""Entrypoint del modo autonomo.

Uso:
  python -m bookforge.daemon            # usa data/autonomy.json
  python -m bookforge.daemon --once     # un solo tick (debug/cron)

La configuracion se edita en data/autonomy.json o via API /autonomy.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from bookforge.core.llm import LLMClient
from bookforge.core.models import Book, BookState
from bookforge.core.storage import BookRepository, CostTracker, init_db
from bookforge.orchestrator.autonomy import AutonomousDaemon, AutonomyConfig
from bookforge.orchestrator.runner import produce_book
from bookforge.pipelines.p1_market.pipeline import MarketIntelligencePipeline
from bookforge.pipelines.p2_production.pipeline import ProductionPipeline

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")


def build_daemon() -> AutonomousDaemon:
    init_db()
    cfg = AutonomyConfig.load()
    repo = BookRepository()
    costs = CostTracker()
    llm = LLMClient(cost_tracker=costs)
    p1 = MarketIntelligencePipeline(llm)
    p2 = ProductionPipeline(llm)

    async def scout(interests: list[str], line: str) -> list[Book]:
        briefs = await p1.run(interests, top_n_to_brief=2)
        created: list[Book] = []
        for brief in briefs:
            book = Book(line_slug=line, title=brief.working_title,
                        niche=brief.niche, market_brief=brief)
            repo.save(book)
            repo.log_event(book.id, "created_by_daemon",
                           f"score={brief.viability_score} {brief.verdict}")
            created.append(book)
        return created

    async def produce(book_id: str) -> None:
        await produce_book(book_id, repo, p2)

    async def postproduce(book_id: str) -> None:
        """Nivel 2: visual + formatting. Publicacion queda en
        READY_TO_PUBLISH; el paso a PUBLISHING respeta kdp_dry_run y por
        ahora es manual via API."""
        book = repo.get(book_id)
        if book is None or book.manuscript is None:
            return
        from bookforge.core.config import settings
        from bookforge.pipelines.p4_publish.pipeline import EpubBuilder
        # Portada requiere BF_OPENAI_API_KEY; si falta, seguimos sin portada
        # y se registra para hacerla manual.
        cover_path = None
        try:
            if settings.openai_api_key:
                import yaml
                from bookforge.core.models import BrandKit
                from bookforge.pipelines.p3_visual.pipeline import CoverPipeline
                brand_file = next(
                    p for p in __import__("pathlib").Path("brands").glob("*.yaml")
                    if yaml.safe_load(p.read_text())["line_slug"] == book.line_slug
                )
                brand = BrandKit.model_validate(
                    yaml.safe_load(brand_file.read_text()))
                result = await CoverPipeline(LLMClient(cost_tracker=costs)).run(
                    book.market_brief, brand, brand.pen_name, book_id)
                cover_path = __import__("pathlib").Path(
                    result["covers"]["kdp_ebook"]["path"])
        except Exception as exc:
            repo.log_event(book_id, "cover_skipped", repr(exc))

        book.transition(BookState.FORMATTING)
        repo.save(book)
        epub = EpubBuilder().build(book.manuscript,
                                   author=book.line_slug,
                                   cover_jpg=cover_path,
                                   book_id=str(book.id))
        repo.log_event(book_id, "epub_built", str(epub))
        book.transition(BookState.READY_TO_PUBLISH)
        repo.save(book)

    return AutonomousDaemon(
        cfg=cfg, repo=repo,
        monthly_spend=costs.total_current_month,
        scout=scout, produce=produce, postproduce=postproduce,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="ejecuta un solo tick y termina")
    args = parser.parse_args()
    daemon = build_daemon()
    if args.once:
        result = asyncio.run(daemon.tick())
        print(result)
        return 0
    asyncio.run(daemon.run_forever())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
