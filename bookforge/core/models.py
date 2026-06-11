"""Contratos de datos de BookForge v2.

Todo pipeline consume y produce estos modelos. Si un contrato no se
cumple, el pipeline falla rapido (ValidationError) en lugar de propagar
basura aguas abajo.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Estado del libro (state machine)
# ---------------------------------------------------------------------------

class BookState(str, Enum):
    RESEARCH = "research"
    BRIEF_APPROVED = "brief_approved"        # gate humano 1
    OUTLINE = "outline"
    DRAFTING = "drafting"
    EDITING = "editing"
    QA_SCORING = "qa_scoring"
    QA_FAILED = "qa_failed"
    HUMAN_REVIEW = "human_review"            # gate humano 2 (obligatorio)
    VISUAL_PRODUCTION = "visual_production"
    FORMATTING = "formatting"
    READY_TO_PUBLISH = "ready_to_publish"
    PUBLISHING = "publishing"
    LIVE = "live"
    MARKETING_ACTIVE = "marketing_active"
    ARCHIVED = "archived"
    REJECTED = "rejected"


# Transiciones validas. Cualquier otra es un bug y debe explotar.
VALID_TRANSITIONS: dict[BookState, set[BookState]] = {
    BookState.RESEARCH: {BookState.BRIEF_APPROVED, BookState.REJECTED},
    BookState.BRIEF_APPROVED: {BookState.OUTLINE},
    BookState.OUTLINE: {BookState.DRAFTING},
    BookState.DRAFTING: {BookState.EDITING},
    BookState.EDITING: {BookState.QA_SCORING},
    BookState.QA_SCORING: {BookState.HUMAN_REVIEW, BookState.QA_FAILED},
    BookState.QA_FAILED: {BookState.EDITING, BookState.HUMAN_REVIEW,
                          BookState.ARCHIVED},
    BookState.HUMAN_REVIEW: {BookState.VISUAL_PRODUCTION, BookState.EDITING,
                             BookState.ARCHIVED},
    BookState.VISUAL_PRODUCTION: {BookState.FORMATTING},
    BookState.FORMATTING: {BookState.READY_TO_PUBLISH},
    BookState.READY_TO_PUBLISH: {BookState.PUBLISHING},
    BookState.PUBLISHING: {BookState.LIVE, BookState.READY_TO_PUBLISH},
    BookState.LIVE: {BookState.MARKETING_ACTIVE, BookState.ARCHIVED},
    BookState.MARKETING_ACTIVE: {BookState.LIVE, BookState.ARCHIVED},
    BookState.ARCHIVED: set(),
    BookState.REJECTED: set(),
}


class InvalidTransition(Exception):
    pass


def assert_transition(current: BookState, target: BookState) -> None:
    if target not in VALID_TRANSITIONS[current]:
        raise InvalidTransition(
            f"Transicion invalida: {current.value} -> {target.value}"
        )


# ---------------------------------------------------------------------------
# Pipeline 1: Market Intelligence
# ---------------------------------------------------------------------------

BookType = Literal[
    "nonfiction_howto", "workbook", "guide", "low_content", "fiction_novella"
]


class AudiencePersona(BaseModel):
    name: str
    age_range: str
    countries: list[str]
    pain_points: list[str]
    vocabulary_notes: str
    buying_objections: list[str]


class PriceStrategy(BaseModel):
    ebook_usd: Decimal
    paperback_usd: Optional[Decimal] = None
    launch_discount_pct: int = 0
    kdp_select: bool = False


class CompetitorSnapshot(BaseModel):
    title: str
    asin: Optional[str] = None
    est_bsr: Optional[int] = None
    price_usd: Optional[Decimal] = None
    review_count: Optional[int] = None
    rating: Optional[float] = None
    weaknesses: list[str] = Field(default_factory=list)


class MarketBrief(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    created_at: datetime = Field(default_factory=utcnow)
    niche: str
    book_type: BookType
    working_title: str
    target_persona: AudiencePersona
    keywords: list[str] = Field(min_length=1, max_length=7)
    categories: list[str] = Field(min_length=1, max_length=3)
    price_strategy: PriceStrategy
    competitors: list[CompetitorSnapshot] = Field(default_factory=list)
    competitor_gaps: list[str]
    differentiation: str
    viability_score: float = Field(ge=0, le=100)
    verdict: Literal["GO", "NO_GO"]
    verdict_reasoning: str
    est_monthly_revenue_low: Decimal = Decimal("0")
    est_monthly_revenue_high: Decimal = Decimal("0")

    @field_validator("keywords")
    @classmethod
    def keywords_nonempty(cls, v: list[str]) -> list[str]:
        cleaned = [k.strip() for k in v if k.strip()]
        if not cleaned:
            raise ValueError("keywords vacias")
        return cleaned


# ---------------------------------------------------------------------------
# Pipeline 2: Produccion
# ---------------------------------------------------------------------------

class ChapterOutline(BaseModel):
    number: int
    title: str
    thesis: str
    key_points: list[str]
    target_words: int = Field(ge=300, le=8000)


class BookBible(BaseModel):
    """Ancla de continuidad: lo que todo Chapter Writer debe respetar."""
    promise_to_reader: str
    tone: str
    style_rules: list[str]
    terminology: dict[str, str] = Field(default_factory=dict)
    recurring_examples: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)


class BookOutline(BaseModel):
    title: str
    subtitle: str
    bible: BookBible
    chapters: list[ChapterOutline] = Field(min_length=3)


class Chapter(BaseModel):
    number: int
    title: str
    content_md: str
    word_count: int = 0

    def model_post_init(self, __context) -> None:
        if not self.word_count:
            object.__setattr__(self, "word_count", len(self.content_md.split()))


class Manuscript(BaseModel):
    title: str
    subtitle: str
    chapters: list[Chapter]
    revision: int = 0

    @property
    def total_words(self) -> int:
        return sum(c.word_count for c in self.chapters)

    def to_markdown(self) -> str:
        parts = [f"# {self.title}", f"## {self.subtitle}", ""]
        for ch in self.chapters:
            parts.append(f"\n# Chapter {ch.number}: {ch.title}\n")
            parts.append(ch.content_md)
        return "\n".join(parts)


class QAScore(BaseModel):
    structure: float = Field(ge=0, le=20)
    depth_value: float = Field(ge=0, le=25)
    prose_quality: float = Field(ge=0, le=20)
    originality: float = Field(ge=0, le=20)
    brief_compliance: float = Field(ge=0, le=15)
    feedback: list[str] = Field(default_factory=list)

    @property
    def total(self) -> float:
        return round(
            self.structure + self.depth_value + self.prose_quality
            + self.originality + self.brief_compliance, 2
        )

    def passes(self, threshold: float = 80.0) -> bool:
        return self.total >= threshold


# ---------------------------------------------------------------------------
# Pipeline 3: Marca y visual
# ---------------------------------------------------------------------------

class BrandKit(BaseModel):
    pen_name: str
    line_slug: str
    palette: list[str]                  # hex
    title_font: str
    body_font: str
    cover_style_prompt: str             # ancla de estilo para imagegen
    tone: str


class CoverSpec(BaseModel):
    channel: Literal["kdp_ebook", "kdp_paperback", "google_play", "d2d"]
    width_px: int
    height_px: int
    notes: str = ""


COVER_SPECS: dict[str, CoverSpec] = {
    "kdp_ebook": CoverSpec(channel="kdp_ebook", width_px=1600, height_px=2560,
                           notes="ratio 1.6:1, RGB, <50MB"),
    "google_play": CoverSpec(channel="google_play", width_px=1400,
                             height_px=2100, notes="min 1024px lado menor"),
    "d2d": CoverSpec(channel="d2d", width_px=1600, height_px=2400,
                     notes="ratio 1.5:1 recomendado"),
}


# ---------------------------------------------------------------------------
# Pipeline 5: Marketing flags
# ---------------------------------------------------------------------------

class OrganicFlags(BaseModel):
    enabled: bool = False
    pinterest: bool = False
    tiktok: bool = False
    instagram: bool = False
    twitter: bool = False
    email: bool = False
    posts_per_day_max: int = 3


class PaidFlags(BaseModel):
    enabled: bool = False
    amazon_ads: bool = False
    meta_ads: bool = False
    daily_budget_usd: Decimal = Decimal("5.00")
    max_acos_pct: float = 70.0
    auto_pause_on_breach: bool = True


class MarketingFlags(BaseModel):
    master: bool = False
    organic: OrganicFlags = Field(default_factory=OrganicFlags)
    paid: PaidFlags = Field(default_factory=PaidFlags)

    def channel_active(self, channel: str) -> bool:
        """Un canal solo esta activo si master + grupo + canal estan ON."""
        if not self.master:
            return False
        if hasattr(self.organic, channel):
            return self.organic.enabled and getattr(self.organic, channel)
        if hasattr(self.paid, channel):
            return self.paid.enabled and getattr(self.paid, channel)
        return False


# ---------------------------------------------------------------------------
# Libro (entidad raiz)
# ---------------------------------------------------------------------------

class Book(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    created_at: datetime = Field(default_factory=utcnow)
    line_slug: str
    title: str
    subtitle: Optional[str] = None
    niche: str
    target_markets: list[str] = Field(default_factory=lambda: ["US", "UK"])
    state: BookState = BookState.RESEARCH
    market_brief: Optional[MarketBrief] = None
    outline: Optional[BookOutline] = None
    manuscript: Optional[Manuscript] = None
    qa_score: Optional[QAScore] = None
    qa_cycles: int = 0
    cost_usd: Decimal = Decimal("0")
    asin: Optional[str] = None
    formats: list[str] = Field(default_factory=lambda: ["ebook"])

    def transition(self, target: BookState) -> None:
        assert_transition(self.state, target)
        self.state = target
