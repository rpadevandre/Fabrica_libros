from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bookforge.core import storage
from bookforge.core.models import Book, BookState


def make_repo(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db", future=True)
    storage.Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    return storage.BookRepository(sf), storage.CostTracker(sf)


def test_save_and_roundtrip(tmp_path):
    repo, _ = make_repo(tmp_path)
    book = Book(line_slug="ia-press", title="Test", niche="dev")
    repo.save(book)
    loaded = repo.get(book.id)
    assert loaded is not None
    assert loaded.title == "Test"
    assert loaded.state == BookState.RESEARCH


def test_cost_accumulates_on_book(tmp_path):
    repo, costs = make_repo(tmp_path)
    book = Book(line_slug="x", title="T", niche="n")
    repo.save(book)
    costs.record(book_id=str(book.id), phase="p2.writer", model="m",
                 cost_usd=Decimal("0.15"), input_tokens=100, output_tokens=200)
    costs.record(book_id=str(book.id), phase="p2.qa", model="m",
                 cost_usd=Decimal("0.05"), input_tokens=50, output_tokens=50)
    assert costs.total_for_book(str(book.id)) == Decimal("0.20")
    assert repo.get(book.id).cost_usd == Decimal("0.20")


def test_list_by_state(tmp_path):
    repo, _ = make_repo(tmp_path)
    b1 = Book(line_slug="x", title="A", niche="n")
    b2 = Book(line_slug="x", title="B", niche="n")
    b2.transition(BookState.BRIEF_APPROVED)
    repo.save(b1)
    repo.save(b2)
    assert len(repo.list_by_state(BookState.RESEARCH)) == 1
    assert len(repo.list_by_state()) == 2
