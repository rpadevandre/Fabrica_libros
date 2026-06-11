"""Runner de produccion: avanza un libro de BRIEF_APPROVED a HUMAN_REVIEW.

Compartido por la API (background task) y el daemon autonomo, para que
exista UNA sola implementacion del flujo de produccion.
"""
from __future__ import annotations

from bookforge.core.config import settings
from bookforge.core.models import BookState
from bookforge.core.storage import BookRepository
from bookforge.pipelines.p2_production.pipeline import ProductionPipeline


async def produce_book(book_id: str, repo: BookRepository,
                       p2: ProductionPipeline) -> None:
    """Precondicion: el libro esta en BRIEF_APPROVED."""
    book = repo.get(book_id)
    if book is None or book.market_brief is None:
        return
    try:
        book.transition(BookState.OUTLINE)
        repo.save(book)
        outline = await p2.build_outline(book.market_brief, book_id)
        book.outline = outline
        book.title, book.subtitle = outline.title, outline.subtitle
        book.transition(BookState.DRAFTING)
        repo.save(book)

        ms = await p2.draft_manuscript(outline, book_id)
        book.manuscript = ms
        book.transition(BookState.EDITING)
        repo.save(book)

        ms = await p2.continuity_pass(ms, outline, book_id)
        ms = await p2.line_edit_pass(ms, outline, book_id)
        book.manuscript = ms
        book.transition(BookState.QA_SCORING)
        repo.save(book)

        score = await p2.qa_score(ms, book.market_brief, book_id)
        book.qa_score = score
        while (not score.passes(settings.qa_threshold)
               and book.qa_cycles < settings.max_qa_cycles):
            book.qa_cycles += 1
            book.transition(BookState.QA_FAILED)
            repo.save(book)
            ms = await p2._revise_with_feedback(ms, score, book_id)
            book.manuscript = ms
            book.transition(BookState.EDITING)
            book.transition(BookState.QA_SCORING)
            repo.save(book)
            score = await p2.qa_score(ms, book.market_brief, book_id)
            book.qa_score = score

        if score.passes(settings.qa_threshold):
            book.transition(BookState.HUMAN_REVIEW)
        else:
            book.transition(BookState.QA_FAILED)
        repo.save(book)
        repo.log_event(book.id, "production_done",
                       f"qa={score.total} words={ms.total_words}")
    except Exception as exc:
        repo.log_event(book_id, "production_error", repr(exc))
        raise
