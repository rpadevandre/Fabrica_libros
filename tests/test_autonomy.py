from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bookforge.core.models import (
    AudiencePersona, Book, BookState, MarketBrief, PriceStrategy, QAScore,
)
from bookforge.orchestrator.autonomy import (
    AutonomousDaemon, AutonomyConfig, should_auto_approve_brief,
    should_auto_approve_manuscript,
)


def make_brief(score: float, verdict: str = "GO") -> MarketBrief:
    return MarketBrief(
        niche="n", book_type="guide", working_title="T",
        target_persona=AudiencePersona(
            name="p", age_range="30", countries=["US"], pain_points=[],
            vocabulary_notes="", buying_objections=[]),
        keywords=["k"], categories=["c"],
        price_strategy=PriceStrategy(ebook_usd=Decimal("4.99")),
        competitor_gaps=[], differentiation="d",
        viability_score=score, verdict=verdict, verdict_reasoning="r",
    )


def make_qa(total_target: float) -> QAScore:
    # reparte el total entre componentes respetando maximos
    return QAScore(structure=min(20, total_target * 0.2),
                   depth_value=min(25, total_target * 0.25),
                   prose_quality=min(20, total_target * 0.2),
                   originality=min(20, total_target * 0.2),
                   brief_compliance=min(15, total_target * 0.15))


class FakeRepo:
    def __init__(self):
        self.books: dict[str, Book] = {}
        self.events: list[tuple] = []

    def save(self, book: Book):
        self.books[str(book.id)] = book

    def get(self, book_id):
        return self.books.get(str(book_id))

    def list_by_state(self, state=None):
        items = list(self.books.values())
        return [b for b in items if state is None or b.state == state]

    def log_event(self, book_id, event, detail=""):
        self.events.append((str(book_id), event, detail))


def make_daemon(cfg: AutonomyConfig, repo: FakeRepo,
                spend=Decimal("0"), scout_result=None):
    produced: list[str] = []
    postproduced: list[str] = []
    notifications: list[tuple] = []

    async def scout(interests, line):
        return scout_result or []

    async def produce(book_id: str):
        produced.append(book_id)
        book = repo.get(book_id)
        # simular produccion exitosa hasta HUMAN_REVIEW
        for st in (BookState.OUTLINE, BookState.DRAFTING, BookState.EDITING,
                   BookState.QA_SCORING, BookState.HUMAN_REVIEW):
            book.transition(st)
        repo.save(book)

    async def postproduce(book_id: str):
        postproduced.append(book_id)

    async def notifier(event, payload):
        notifications.append((event, payload))

    daemon = AutonomousDaemon(
        cfg=cfg, repo=repo, monthly_spend=lambda: spend,
        scout=scout, produce=produce, postproduce=postproduce,
        notifier=notifier,
    )
    return daemon, produced, postproduced, notifications


# ---------------------------------------------------------------------------
# Politicas puras
# ---------------------------------------------------------------------------

def test_brief_policy_respects_level_and_threshold():
    cfg = AutonomyConfig(level=0)
    assert not should_auto_approve_brief(make_brief(99), cfg)
    cfg = AutonomyConfig(level=1, brief_auto_threshold=85)
    assert should_auto_approve_brief(make_brief(90), cfg)
    assert not should_auto_approve_brief(make_brief(80), cfg)
    assert not should_auto_approve_brief(make_brief(90, "NO_GO"), cfg)


def test_manuscript_policy_requires_level_2():
    book = Book(line_slug="x", title="T", niche="n")
    book.qa_score = make_qa(95)
    assert not should_auto_approve_manuscript(
        book, AutonomyConfig(level=1))
    assert should_auto_approve_manuscript(
        book, AutonomyConfig(level=2, manuscript_auto_threshold=90))
    book.qa_score = make_qa(85)
    assert not should_auto_approve_manuscript(
        book, AutonomyConfig(level=2, manuscript_auto_threshold=90))


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_level_0_does_nothing():
    repo = FakeRepo()
    book = Book(line_slug="x", title="T", niche="n",
                market_brief=make_brief(99))
    repo.save(book)
    daemon, produced, _, _ = make_daemon(AutonomyConfig(level=0), repo)
    result = await daemon.tick()
    assert result["actions"] == ["level_0_idle"]
    assert produced == []


@pytest.mark.asyncio
async def test_budget_exceeded_blocks_everything():
    repo = FakeRepo()
    repo.save(Book(line_slug="x", title="T", niche="n",
                   market_brief=make_brief(99)))
    cfg = AutonomyConfig(level=2, monthly_budget_usd=Decimal("50"))
    daemon, produced, _, notes = make_daemon(cfg, repo,
                                             spend=Decimal("50.01"))
    result = await daemon.tick()
    assert result["actions"] == ["budget_exceeded_idle"]
    assert produced == []
    assert notes[0][0] == "budget_exceeded"


@pytest.mark.asyncio
async def test_level_1_full_flow_stops_at_human_review():
    repo = FakeRepo()
    book = Book(line_slug="x", title="T", niche="n",
                market_brief=make_brief(90))
    repo.save(book)
    cfg = AutonomyConfig(level=1, brief_auto_threshold=85)
    daemon, produced, postproduced, notes = make_daemon(cfg, repo)
    await daemon.tick()
    assert produced == [str(book.id)]
    assert repo.get(book.id).state == BookState.HUMAN_REVIEW
    assert postproduced == []  # gate 2 humano
    # segundo tick: notifica que espera revision humana, no avanza
    await daemon.tick()
    assert repo.get(book.id).state == BookState.HUMAN_REVIEW
    assert any(e == "awaiting_human_review" for e, _ in notes)


@pytest.mark.asyncio
async def test_level_2_auto_approves_high_qa():
    repo = FakeRepo()
    book = Book(line_slug="x", title="T", niche="n",
                market_brief=make_brief(90))
    book.transition(BookState.BRIEF_APPROVED)
    for st in (BookState.OUTLINE, BookState.DRAFTING, BookState.EDITING,
               BookState.QA_SCORING, BookState.HUMAN_REVIEW):
        book.transition(st)
    book.qa_score = make_qa(95)
    repo.save(book)
    cfg = AutonomyConfig(level=2, manuscript_auto_threshold=90)
    daemon, _, postproduced, _ = make_daemon(cfg, repo)
    await daemon.tick()
    assert postproduced == [str(book.id)]
    assert repo.get(book.id).state == BookState.VISUAL_PRODUCTION


@pytest.mark.asyncio
async def test_level_2_low_qa_still_waits_for_human():
    repo = FakeRepo()
    book = Book(line_slug="x", title="T", niche="n",
                market_brief=make_brief(90))
    for st in (BookState.BRIEF_APPROVED, BookState.OUTLINE,
               BookState.DRAFTING, BookState.EDITING, BookState.QA_SCORING,
               BookState.HUMAN_REVIEW):
        book.transition(st)
    book.qa_score = make_qa(82)
    repo.save(book)
    cfg = AutonomyConfig(level=2, manuscript_auto_threshold=90)
    daemon, _, postproduced, notes = make_daemon(cfg, repo)
    await daemon.tick()
    assert postproduced == []
    assert repo.get(book.id).state == BookState.HUMAN_REVIEW
    assert any(e == "awaiting_human_review" for e, _ in notes)


@pytest.mark.asyncio
async def test_max_books_in_flight_respected():
    repo = FakeRepo()
    briefs = [make_brief(95) for _ in range(4)]
    for brief in briefs:
        repo.save(Book(line_slug="x", title="T", niche="n",
                       market_brief=brief))
    cfg = AutonomyConfig(level=1, max_books_in_flight=2)
    daemon, produced, _, _ = make_daemon(cfg, repo)
    await daemon.tick()
    # solo 2 entran a produccion; los otros 2 siguen en RESEARCH
    research = repo.list_by_state(BookState.RESEARCH)
    assert len(produced) == 2
    assert len(research) == 2


@pytest.mark.asyncio
async def test_scout_interval_and_monthly_cap():
    repo = FakeRepo()
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    new_book = Book(line_slug="x", title="S", niche="n",
                    market_brief=make_brief(50, "NO_GO"))

    async def scout(interests, line):
        repo.save(new_book)
        return [new_book]

    async def produce(book_id):
        pass

    daemon = AutonomousDaemon(
        cfg=AutonomyConfig(level=1, seed_interests=["dev"],
                           max_new_books_per_month=1,
                           scout_interval_hours=168),
        repo=repo, monthly_spend=lambda: Decimal("0"),
        scout=scout, produce=produce,
        notifier=None, clock=lambda: now,
    )
    daemon.notifier = daemon._default_notifier

    r1 = await daemon.tick()
    assert any(a.startswith("scouted") for a in r1["actions"])
    # mismo dia: no vuelve a scoutear (intervalo) ni crea mas (cap mensual)
    r2 = await daemon.tick()
    assert not any(a.startswith("scouted") for a in r2["actions"])
