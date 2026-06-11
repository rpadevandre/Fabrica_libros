from decimal import Decimal

import pytest
from pydantic import ValidationError

from bookforge.core.llm import LLMError, extract_json
from bookforge.core.models import (
    Chapter, Manuscript, MarketBrief, QAScore,
)
from bookforge.pipelines.p2_production.pipeline import ProductionPipeline


def test_extract_json_with_fences():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_preamble():
    assert extract_json('Claro, aqui esta:\n{"a": [1, 2]} y listo') == {"a": [1, 2]}


def test_extract_json_fails_loudly():
    with pytest.raises(LLMError):
        extract_json("no hay json aqui")


def test_qa_score_threshold():
    score = QAScore(structure=18, depth_value=22, prose_quality=17,
                    originality=16, brief_compliance=12)
    assert score.total == 85.0
    assert score.passes(80)
    assert not score.passes(90)


def test_manuscript_word_count_and_md():
    ms = Manuscript(title="T", subtitle="S", chapters=[
        Chapter(number=1, title="One", content_md="hello world foo"),
        Chapter(number=2, title="Two", content_md="bar baz"),
    ])
    assert ms.total_words == 5
    md = ms.to_markdown()
    assert "# Chapter 1: One" in md and "# Chapter 2: Two" in md


def test_ai_tell_detection():
    ms = Manuscript(title="T", subtitle="S", chapters=[
        Chapter(number=1, title="One",
                content_md="Let's delve into this tapestry. Delve again."),
    ])
    tells = ProductionPipeline.detect_ai_tells(ms)
    assert tells["delve"] == 2
    assert tells["tapestry"] == 1


def test_market_brief_validates_keywords():
    with pytest.raises(ValidationError):
        MarketBrief(
            niche="x", book_type="guide", working_title="t",
            target_persona={"name": "p", "age_range": "30-40",
                            "countries": ["US"], "pain_points": [],
                            "vocabulary_notes": "", "buying_objections": []},
            keywords=["   "], categories=["c"],
            price_strategy={"ebook_usd": "4.99"},
            competitor_gaps=[], differentiation="d",
            viability_score=80, verdict="GO", verdict_reasoning="r",
        )
