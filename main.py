"""
quiz_news — News to Quiz-worthy Facts Pipeline

Usage:
    python main.py                    # analyze last week from stored articles
    python main.py --live             # fetch live from RSS (no DB)
    python main.py --store-only       # fetch RSS and store, no LLM (used by cron)
    python main.py --weeks-ago 2      # go back 2 weeks
    python main.py --min 7            # only show facts scored 7+
    python main.py --top 5            # show top 5 per category
    python main.py --dry-run          # fetch only, print titles, no LLM
    python main.py --stats            # show DB stats
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()

from fetcher import fetch_all, current_week_range
from analyzer import analyze, ScoredFact
from store import save_articles, get_articles, store_stats, prune_articles

CATEGORY_LABEL = {"cz": "Czech", "world": "World"}
CATEGORY_ICON  = {"cz": "CZ",    "world": "WORLD"}


def print_results(facts: list[ScoredFact], weeks_ago: int, top: int | None = None) -> None:
    by_category: dict[str, list[ScoredFact]] = {}
    for f in facts:
        by_category.setdefault(f.category, []).append(f)

    week_start, week_end = current_week_range(weeks_ago=weeks_ago)
    week_last  = week_end - datetime.timedelta(days=1)
    week_label = f"{week_start.day}.{week_start.month}. – {week_last.day}.{week_last.month}.{week_last.year}"

    print("\n" + "=" * 60)
    print(f"  Quiz-worthy facts  ({week_label})")
    print("=" * 60)

    for cat in ("cz", "world"):
        items = by_category.get(cat, [])
        if not items:
            continue
        if top:
            items = items[:top]
        print(f"\n[{CATEGORY_ICON[cat]}] {CATEGORY_LABEL[cat]}:")
        print("-" * 40)
        for f in items:
            print(f"  [{f.score}/10]  {f.fact}")
            print(f"         ({f.source} — {f.reason})")
            if f.url:
                print(f"         {f.url}")

    print("\n" + "=" * 60)
    print(f"  Total quiz-worthy facts: {sum(len(v) for v in by_category.values())}")
    print("=" * 60 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Quiz-worthy news fact extractor")
    parser.add_argument("--min",        type=int,  default=6,     help="Minimum quiz score (1-10)")
    parser.add_argument("--top",        type=int,  default=None,  help="Max facts per category")
    parser.add_argument("--weeks-ago",  type=int,  default=1,     help="1=last week (default), 0=this week")
    parser.add_argument("--live",       action="store_true",      help="Fetch live from RSS instead of DB")
    parser.add_argument("--store-only", action="store_true",      help="Fetch RSS and store to DB, skip LLM")
    parser.add_argument("--dry-run",    action="store_true",      help="Fetch only, print titles, no LLM")
    parser.add_argument("--stats",      action="store_true",      help="Show DB stats and exit")
    parser.add_argument("--save",       type=str, default=None,   help="Save scored facts to this JSON file")
    parser.add_argument("--prune",      action="store_true",      help="Remove last week's articles from store after analysis")
    args = parser.parse_args()

    # Stats mode
    if args.stats:
        s = store_stats()
        print(f"DB: {s['total']} articles  |  {s['oldest']} → {s['newest']}")
        return

    # Store-only mode (cron job): fetch current day's articles and save
    if args.store_only:
        print("Fetching and storing articles (no date filter)...")
        articles = fetch_all(week_only=False)
        n = save_articles(articles)
        print(f"  Stored {n} new articles ({len(articles)} fetched, duplicates skipped)")
        return

    need_api = not args.dry_run
    if need_api and not os.environ.get("GROQ_API_KEY"):
        print("Error: GROQ_API_KEY not set. Add it to .env or export it.")
        sys.exit(1)

    week_start, week_end = current_week_range(weeks_ago=args.weeks_ago)

    # Source: DB (default) or live RSS
    if args.live:
        print(f"Fetching live RSS (week from {week_start.strftime('%a %d.%m.')})...")
        articles = fetch_all(week_only=True, weeks_ago=args.weeks_ago)
    else:
        print(f"Reading from DB (week {week_start.strftime('%d.%m.')} – {week_end.strftime('%d.%m.')})...")
        articles = get_articles(week_start, week_end)

    print(f"  {len(articles)} articles")

    if args.dry_run:
        for a in articles:
            date_str = a.published.strftime("%d.%m.") if a.published else "?"
            print(f"  [{a.category}] [{date_str}] [{a.source}] {a.title}")
        return

    print(f"Scoring facts (min score: {args.min})...")
    facts = analyze(articles, min_score=args.min)
    print(f"  {len(facts)} facts passed the threshold")

    print_results(facts, weeks_ago=args.weeks_ago, top=args.top)

    if args.prune:
        removed = prune_articles(keep_from=week_end)
        print(f"Pruned {removed} articles older than {week_end.strftime('%d.%m.%Y')} from store")

    if args.save:
        out = {
            "week_start": week_start.isoformat(),
            "week_end":   week_end.isoformat(),
            "generated":  datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "facts": [
                {
                    "score":    f.score,
                    "fact":     f.fact,
                    "reason":   f.reason,
                    "source":   f.source,
                    "category": f.category,
                    "url":      f.url,
                }
                for f in facts
            ],
        }
        Path(args.save).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved {len(facts)} facts to {args.save}")


if __name__ == "__main__":
    main()
