import pytest

from bookforge.core.models import (
    Book, BookState, InvalidTransition, assert_transition,
)


def test_happy_path_full_lifecycle():
    book = Book(line_slug="x", title="T", niche="n")
    path = [
        BookState.BRIEF_APPROVED, BookState.OUTLINE, BookState.DRAFTING,
        BookState.EDITING, BookState.QA_SCORING, BookState.HUMAN_REVIEW,
        BookState.VISUAL_PRODUCTION, BookState.FORMATTING,
        BookState.READY_TO_PUBLISH, BookState.PUBLISHING, BookState.LIVE,
        BookState.MARKETING_ACTIVE,
    ]
    for target in path:
        book.transition(target)
    assert book.state == BookState.MARKETING_ACTIVE


def test_qa_failure_loop():
    assert_transition(BookState.QA_SCORING, BookState.QA_FAILED)
    assert_transition(BookState.QA_FAILED, BookState.EDITING)


def test_cannot_skip_human_review():
    with pytest.raises(InvalidTransition):
        assert_transition(BookState.QA_SCORING, BookState.VISUAL_PRODUCTION)


def test_cannot_publish_from_drafting():
    with pytest.raises(InvalidTransition):
        assert_transition(BookState.DRAFTING, BookState.PUBLISHING)


def test_archived_is_terminal():
    with pytest.raises(InvalidTransition):
        assert_transition(BookState.ARCHIVED, BookState.LIVE)
