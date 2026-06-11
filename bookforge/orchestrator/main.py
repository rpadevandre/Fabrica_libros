"""Orchestrator — API de control de la fabrica.

Endpoints:
  POST /research                  lanza Pipeline 1 (devuelve briefs)
  POST /books                     crea libro desde un brief
  POST /books/{id}/approve-brief  gate humano 1 -> arranca produccion (bg)
  POST /books/{id}/approve-manuscript  gate humano 2 -> visual+formatting
  GET  /books / GET /books/{id}
  GET  /books/{id}/manuscript     descarga markdown para revision
  GET  /flags  PUT /flags/{channel}?value=  POST /flags/kill
  GET  /costs/{book_id}
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from bookforge.core.config import settings
from bookforge.core.llm import LLMClient
from bookforge.core.models import Book, BookState, MarketBrief
from bookforge.core.storage import BookRepository, CostTracker, init_db
from bookforge.orchestrator.flags import get_flag_store, kill_switch, set_channel
from bookforge.pipelines.p1_market.pipeline import MarketIntelligencePipeline
from bookforge.pipelines.p2_production.pipeline import ProductionPipeline
from bookforge.orchestrator.runner import produce_book
from bookforge.orchestrator.autonomy import AutonomyConfig

app = FastAPI(title="BookForge v2", version="0.1.0")

repo = BookRepository()
costs = CostTracker()
llm = LLMClient(cost_tracker=costs)
p1 = MarketIntelligencePipeline(llm)
p2 = ProductionPipeline(llm)
flag_store = get_flag_store()


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# Pipeline 1
# ---------------------------------------------------------------------------

class ResearchRequest(BaseModel):
    seed_interests: list[str]
    top_n: int = 3


@app.post("/research")
async def run_research(req: ResearchRequest) -> list[MarketBrief]:
    return await p1.run(req.seed_interests, top_n_to_brief=req.top_n)


class CreateBookRequest(BaseModel):
    line_slug: str
    brief: MarketBrief


@app.post("/books")
def create_book(req: CreateBookRequest) -> Book:
    book = Book(
        line_slug=req.line_slug,
        title=req.brief.working_title,
        niche=req.brief.niche,
        market_brief=req.brief,
    )
    repo.save(book)
    repo.log_event(book.id, "created", f"score={req.brief.viability_score}")
    return book


# ---------------------------------------------------------------------------
# Gates y produccion
# ---------------------------------------------------------------------------

@app.post("/books/{book_id}/approve-brief")
async def approve_brief(book_id: str, bg: BackgroundTasks) -> dict:
    book = repo.get(book_id)
    if book is None:
        raise HTTPException(404)
    if book.state != BookState.RESEARCH:
        raise HTTPException(409, f"Estado actual: {book.state}")
    if book.market_brief and book.market_brief.verdict == "NO_GO":
        raise HTTPException(409, "El brief es NO_GO. Crea otro o ajusta.")
    book.transition(BookState.BRIEF_APPROVED)
    repo.save(book)
    repo.log_event(book.id, "brief_approved", "gate humano 1")
    bg.add_task(produce_book, book_id, repo, p2)
    return {"status": "production_started", "book_id": book_id}


@app.post("/books/{book_id}/approve-manuscript")
def approve_manuscript(book_id: str) -> dict:
    book = repo.get(book_id)
    if book is None:
        raise HTTPException(404)
    if book.state != BookState.HUMAN_REVIEW:
        raise HTTPException(409, f"Estado actual: {book.state}")
    book.transition(BookState.VISUAL_PRODUCTION)
    repo.save(book)
    repo.log_event(book.id, "manuscript_approved", "gate humano 2")
    return {"status": "approved", "next": "visual_production"}


@app.post("/books/{book_id}/reject")
def reject_book(book_id: str, reason: str = "") -> dict:
    book = repo.get(book_id)
    if book is None:
        raise HTTPException(404)
    target = (BookState.REJECTED if book.state == BookState.RESEARCH
              else BookState.ARCHIVED)
    book.transition(target)
    repo.save(book)
    repo.log_event(book.id, "rejected", reason)
    return {"status": target.value}


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------

@app.get("/books")
def list_books(state: BookState | None = None) -> list[dict]:
    return [
        {"id": str(b.id), "title": b.title, "state": b.state.value,
         "qa": b.qa_score.total if b.qa_score else None,
         "cost_usd": str(b.cost_usd), "line": b.line_slug}
        for b in repo.list_by_state(state)
    ]


@app.get("/books/{book_id}")
def get_book(book_id: str) -> Book:
    book = repo.get(book_id)
    if book is None:
        raise HTTPException(404)
    return book


@app.get("/books/{book_id}/manuscript", response_class=PlainTextResponse)
def get_manuscript(book_id: str) -> str:
    book = repo.get(book_id)
    if book is None or book.manuscript is None:
        raise HTTPException(404)
    return book.manuscript.to_markdown()


@app.get("/costs/{book_id}")
def get_costs(book_id: str) -> dict:
    return {"book_id": book_id,
            "total_usd": str(costs.total_for_book(book_id))}


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

@app.get("/flags")
def get_flags():
    return flag_store.get()


@app.put("/flags/{channel}")
def put_flag(channel: str, value: bool):
    try:
        return set_channel(channel, value, flag_store)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/flags/kill")
def kill():
    return kill_switch(flag_store)


# ---------------------------------------------------------------------------
# Autonomia (config; el proceso corre con `python -m bookforge.daemon`)
# ---------------------------------------------------------------------------

@app.get("/autonomy")
def get_autonomy() -> AutonomyConfig:
    return AutonomyConfig.load()


@app.put("/autonomy")
def put_autonomy(cfg: AutonomyConfig) -> AutonomyConfig:
    cfg.save()
    return cfg
