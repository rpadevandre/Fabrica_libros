from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from bookforge.core.models import (
    AudiencePersona,
    Book,
    BookOutline,
    BookBible,
    Chapter,
    ChapterOutline,
    CompetitorSnapshot,
    Manuscript,
    MarketBrief,
    PriceStrategy,
    QAScore,
)
from bookforge.core.storage import BookRepository, CostTracker, init_db
from bookforge.pipelines.p2_production.pipeline import ProductionPipeline
from bookforge.pipelines.p4_publish.pipeline import EpubBuilder, PdfBuilder, build_kdp_plan


def paragraph(topic: str, lesson: str, example: str) -> str:
    return (
        f"{topic} is not a matter of waiting for perfect conditions. It is a practice of "
        f"turning a vague intention into a visible next action. {lesson} The useful question is "
        f"not whether the plan feels impressive, but whether it survives contact with a normal "
        f"Tuesday: limited time, limited energy, and a real obligation already on the calendar.\n\n"
        f"For example, {example}. That small move creates evidence. Evidence makes the next "
        f"decision easier. A creator who repeats this loop for weeks starts to trust the system "
        f"more than the mood of the day."
    )


def make_manuscript() -> Manuscript:
    chapters = [
        Chapter(
            number=1,
            title="Begin With the Smallest Honest Version",
            content_md="\n\n".join([
                "## The promise you can keep",
                paragraph(
                    "Starting",
                    "A tiny honest version beats a beautiful imaginary one because it can be tested today.",
                    "a book idea can become a one-page promise, a rough audience note, and three chapter bullets before it becomes a full manuscript",
                ),
                paragraph(
                    "Scope",
                    "The first draft should protect momentum, not ego.",
                    "instead of planning a 60,000-word masterpiece, write a 5,000-word guide that proves the reader problem is real",
                ),
                "## Exercise\nWrite the sentence: *This book helps [specific reader] solve [specific painful problem] without [common obstacle].* Then remove every vague word.",
            ]),
        ),
        Chapter(
            number=2,
            title="Build a Reader Before You Build a Product",
            content_md="\n\n".join([
                "## A reader is not a demographic",
                paragraph(
                    "Reader research",
                    "A buyer is a person under pressure, not a spreadsheet row.",
                    "a productivity book for developers should speak to missed deadlines, context switching, and the shame of abandoned side projects, not merely to age or income",
                ),
                paragraph(
                    "Positioning",
                    "Strong positioning chooses who the book is not for.",
                    "a beginner-friendly guide can deliberately ignore enterprise managers and focus on solo builders with one hour per night",
                ),
                "## Exercise\nList five sentences your reader might say when frustrated. Use those sentences as chapter anchors.",
            ]),
        ),
        Chapter(
            number=3,
            title="Turn Chapters Into Decisions",
            content_md="\n\n".join([
                "## Chapters must create movement",
                paragraph(
                    "Structure",
                    "A chapter earns its place when the reader can make a better decision after finishing it.",
                    "a chapter about focus should end with a chosen constraint, not just an inspiring quote about attention",
                ),
                paragraph(
                    "Editing",
                    "Good editing removes anything that does not change behavior, belief, or clarity.",
                    "if two sections both say 'be consistent,' one must become an example, a checklist, or disappear",
                ),
                "## Exercise\nFor every chapter, write: *After this chapter, the reader will stop doing X and start doing Y.*",
            ]),
        ),
        Chapter(
            number=4,
            title="Publish as a Learning Loop",
            content_md="\n\n".join([
                "## The launch is feedback",
                paragraph(
                    "Publishing",
                    "A launch is not the end of the work; it is the first honest measurement of the promise.",
                    "ten readers clicking a landing page title teaches more than another week of private rewriting",
                ),
                paragraph(
                    "Iteration",
                    "The next version should be guided by reader questions, refunds, reviews, and the phrases people repeat back to you.",
                    "if readers praise the exercises but ignore the theory, the second edition should become more practical",
                ),
                "## Exercise\nCreate a seven-day launch test: one landing page, three posts, two reader interviews, and one revision decision.",
            ]),
        ),
    ]
    return Manuscript(
        title="The Small Book Operating System",
        subtitle="A practical guide for turning rough ideas into useful books",
        chapters=chapters,
    )


def main() -> None:
    init_db()
    repo = BookRepository()
    costs = CostTracker()
    ms = make_manuscript()

    brief = MarketBrief(
        niche="Practical self-publishing systems for solo builders",
        book_type="guide",
        working_title=ms.title,
        target_persona=AudiencePersona(
            name="Solo Builder",
            age_range="22-40",
            countries=["US", "UK", "Canada", "Spain", "Mexico"],
            pain_points=[
                "has ideas but does not finish books",
                "overbuilds before validating demand",
                "needs a simple repeatable launch loop",
            ],
            vocabulary_notes="Plain, direct, practical language; no guru tone.",
            buying_objections=["too theoretical", "too long", "not enough examples"],
        ),
        keywords=["write a short book", "self publishing system", "solo creator book"],
        categories=["Business Writing", "Authorship", "Entrepreneurship"],
        price_strategy=PriceStrategy(ebook_usd=Decimal("4.99"), paperback_usd=Decimal("9.99"), launch_discount_pct=20, kdp_select=False),
        competitors=[CompetitorSnapshot(title="Generic self-publishing guides", weaknesses=["too broad", "too motivational", "not operational enough"])],
        competitor_gaps=["short practical systems", "validation before full manuscript", "launch feedback loop"],
        differentiation="A compact operating manual focused on finishing and validating useful books, not selling a book-writing service.",
        viability_score=72,
        verdict="GO",
        verdict_reasoning="Useful as a deterministic smoke test and as a possible seed for book-marketing workflows.",
        est_monthly_revenue_low=Decimal("50"),
        est_monthly_revenue_high=Decimal("300"),
    )

    outline = BookOutline(
        title=ms.title,
        subtitle=ms.subtitle,
        bible=BookBible(
            promise_to_reader="Turn a rough idea into a small, useful, publishable book.",
            tone="Practical, direct, calm, non-hype.",
            style_rules=["Use concrete examples", "End chapters with exercises", "Avoid AI-sounding filler"],
            terminology={"small book": "a compact book designed to solve one specific problem"},
            recurring_examples=["solo builders", "short guides", "landing page tests"],
            banned_phrases=["delve", "unlock", "game-changer", "in today's fast-paced world"],
        ),
        chapters=[
            ChapterOutline(number=c.number, title=c.title, thesis=f"Chapter {c.number} teaches one practical decision loop.", key_points=["clarity", "action", "feedback"], target_words=max(300, c.word_count))
            for c in ms.chapters
        ],
    )

    qa = QAScore(
        structure=17,
        depth_value=18,
        prose_quality=17,
        originality=15,
        brief_compliance=13,
        feedback=["Deterministic demo book generated without LLM/API keys; expand chapters before commercial use."],
    )

    book = Book(line_slug="infinity-ascend-press", title=ms.title, niche=brief.niche, market_brief=brief, outline=outline, manuscript=ms, qa_score=qa)
    repo.save(book)
    repo.log_event(book.id, "demo_book_generated", f"words={ms.total_words} qa={qa.total}")

    outdir = Path("data/generated_books") / str(book.id)
    outdir.mkdir(parents=True, exist_ok=True)
    md_path = outdir / "manuscript.md"
    md_path.write_text(ms.to_markdown(), encoding="utf-8")
    brief_path = outdir / "brief.json"
    brief_path.write_text(brief.model_dump_json(indent=2), encoding="utf-8")

    epub_path = EpubBuilder(output_dir=outdir).build(ms, author="Infinity Ascend Press", cover_jpg=None, book_id=str(book.id))
    pdf_path = PdfBuilder(output_dir=outdir).build(ms, author="Infinity Ascend Press", book_id=str(book.id))
    kdp_plan = build_kdp_plan(brief, ms, author="Infinity Ascend Press", epub_path=epub_path, blurb_html="<p>A practical guide for solo builders who want to turn rough ideas into useful short books.</p>")
    plan_path = outdir / "kdp_plan.json"
    plan_path.write_text(__import__("json").dumps(kdp_plan, indent=2), encoding="utf-8")

    tells = ProductionPipeline.detect_ai_tells(ms)
    print(f"book_id={book.id}")
    print(f"title={ms.title}")
    print(f"words={ms.total_words}")
    print(f"qa_total={qa.total}")
    print(f"ai_tells={tells}")
    print(f"markdown={md_path}")
    print(f"epub={epub_path}")
    print(f"pdf={pdf_path}")
    print(f"kdp_plan={plan_path}")


if __name__ == "__main__":
    main()
