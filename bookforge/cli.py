"""CLI de BookForge: opera la fabrica desde terminal.

Uso:
  python -m bookforge.cli research "productivity for developers" "minimalism"
  python -m bookforge.cli books
  python -m bookforge.cli approve-brief <book_id>
  python -m bookforge.cli show <book_id>
  python -m bookforge.cli flags
  python -m bookforge.cli flag master on
  python -m bookforge.cli kill
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from bookforge.core.llm import LLMClient
from bookforge.core.models import Book, BookState
from bookforge.core.storage import BookRepository, CostTracker, init_db
from bookforge.orchestrator.flags import get_flag_store, kill_switch, set_channel
from bookforge.pipelines.p1_market.pipeline import MarketIntelligencePipeline


def main() -> int:
    init_db()
    parser = argparse.ArgumentParser(prog="bookforge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_res = sub.add_parser("research")
    p_res.add_argument("interests", nargs="+")
    p_res.add_argument("--top", type=int, default=2)
    p_res.add_argument("--line", default="default")

    sub.add_parser("books")

    p_show = sub.add_parser("show")
    p_show.add_argument("book_id")

    p_app = sub.add_parser("approve-brief")
    p_app.add_argument("book_id")

    sub.add_parser("flags")
    p_flag = sub.add_parser("flag")
    p_flag.add_argument("channel")
    p_flag.add_argument("value", choices=["on", "off"])
    sub.add_parser("kill")

    args = parser.parse_args()
    repo = BookRepository()
    costs = CostTracker()

    if args.cmd == "research":
        llm = LLMClient(cost_tracker=costs)
        p1 = MarketIntelligencePipeline(llm)
        briefs = asyncio.run(p1.run(args.interests, top_n_to_brief=args.top))
        for brief in briefs:
            book = Book(line_slug=args.line, title=brief.working_title,
                        niche=brief.niche, market_brief=brief)
            repo.save(book)
            print(f"[{brief.verdict}] {brief.viability_score:>5.1f}  "
                  f"{brief.working_title}  -> book {book.id}")
            print(f"    diferenciacion: {brief.differentiation}")
        return 0

    if args.cmd == "books":
        for b in repo.list_by_state():
            qa = f"{b.qa_score.total:.0f}" if b.qa_score else "-"
            print(f"{str(b.id)[:8]}  {b.state.value:<18} qa={qa:<4} "
                  f"${b.cost_usd}  {b.title}")
        return 0

    if args.cmd == "show":
        b = repo.get(args.book_id)
        if b is None:
            print("No existe", file=sys.stderr)
            return 1
        print(json.dumps(b.model_dump(mode="json"), indent=2)[:5000])
        return 0

    if args.cmd == "approve-brief":
        b = repo.get(args.book_id)
        if b is None:
            print("No existe", file=sys.stderr)
            return 1
        b.transition(BookState.BRIEF_APPROVED)
        repo.save(b)
        print("Brief aprobado. Lanza la produccion via API "
              "(POST /books/{id}/approve-brief) o un worker.")
        return 0

    store = get_flag_store()
    if args.cmd == "flags":
        print(store.get().model_dump_json(indent=2))
        return 0
    if args.cmd == "flag":
        flags = set_channel(args.channel, args.value == "on", store)
        print(flags.model_dump_json(indent=2))
        return 0
    if args.cmd == "kill":
        kill_switch(store)
        print("Marketing master = OFF")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
