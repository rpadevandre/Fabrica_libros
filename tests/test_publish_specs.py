from bookforge.pipelines.p4_publish.pipeline import (
    build_kdp_plan, kdp_gutter_inches, kdp_spine_inches,
)
from bookforge.core.models import (
    AudiencePersona, Chapter, Manuscript, MarketBrief, PriceStrategy,
)
from decimal import Decimal
from pathlib import Path
import pytest


def test_gutter_rules():
    assert kdp_gutter_inches(100) == 0.375
    assert kdp_gutter_inches(250) == 0.5
    assert kdp_gutter_inches(450) == 0.625
    assert kdp_gutter_inches(900) == 0.875


def test_spine_width():
    assert abs(kdp_spine_inches(200, "white") - 0.4504) < 0.001


def _brief() -> MarketBrief:
    return MarketBrief(
        niche="dev productivity", book_type="guide", working_title="Deep Focus",
        target_persona=AudiencePersona(
            name="Dev Dan", age_range="25-40", countries=["US", "UK"],
            pain_points=["distractions"], vocabulary_notes="tech",
            buying_objections=["another productivity book"]),
        keywords=["focus for programmers"], categories=["Computers"],
        price_strategy=PriceStrategy(ebook_usd=Decimal("4.99")),
        competitor_gaps=["no code examples"], differentiation="dev-native",
        viability_score=82, verdict="GO", verdict_reasoning="ok",
    )


def test_kdp_plan_forces_ai_disclosure():
    ms = Manuscript(title="Deep Focus", subtitle="Sub", chapters=[
        Chapter(number=1, title="C", content_md="x")])
    plan = build_kdp_plan(_brief(), ms, "Author", Path("a.epub"), "<b>x</b>")
    assert plan["ai_content_disclosure"] is True
    assert plan["keywords"] == ["focus for programmers"]
    assert "amazon.de" in plan["marketplace_prices"]
