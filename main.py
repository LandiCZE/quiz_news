"""
quiz_news — News to Quiz-worthy Facts Pipeline

    python main.py daily              # score yesterday, write days/<date>.json
    python main.py daily --date 2026-10-02
    python main.py daily --dry-run    # fetch + select only, no LLM
    python main.py daily --date 2026-09-28 --archive --budget 60
    python main.py week               # merge + re-rank last week
    python main.py week --weeks-ago 0 # the current week so far
    python main.py week --no-rerank   # merge by daily score, no LLM call
    python main.py prune              # drop day files older than 8 weeks
    python main.py stats              # what is in days/

The daily run does the expensive work: it fetches every source's news sitemap,
picks the best candidates from that one closed day and has the LLM score them.
Only the facts worth keeping are stored, so a day costs ~6 kB rather than the
~1.2 MB of raw articles it was derived from.

The weekly run spends one more LLM call putting the week's best candidates in a
single prompt so they compete head to head, then writes facts/YYYY-Wnn.json for
render.py to turn into index.html.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()

import days
from analyzer import (
    DAILY_MIN_SCORE, KEEP_PER_DAY, SCORE_BUDGET, WEEKLY_TOP,
    analyze, rank_week,
)
from enrich import enrich
from fetcher import current_week_range, fetch_all
from selector import dedupe, select

CATEGORY_LABEL = {"cz": "Czech", "world": "World"}
FACTS_DIR = Path(__file__).parent / "facts"

# Day boundaries follow Prague, not UTC: a story filed at 01:00 Czech time
# belongs to that day's news, but in UTC it would fall into the day before.
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Prague")
except Exception:                                    # pragma: no cover
    LOCAL_TZ = datetime.timezone.utc


def local_date(moment: datetime.datetime) -> datetime.date:
    return moment.astimezone(LOCAL_TZ).date()


# --- daily -------------------------------------------------------------------

def run_daily(args) -> None:
    target = (
        datetime.date.fromisoformat(args.date) if args.date
        else datetime.datetime.now(LOCAL_TZ).date() - datetime.timedelta(days=1)
    )
    print(f"Scoring {target.isoformat()} (Europe/Prague day)")

    if days.load_day(target) and not args.force:
        print(f"  {days.day_path(target).name} already exists — use --force to redo it")
        return

    if args.archive:
        from archive import fetch_archive
        print("  Using archive sitemaps (4 sources, no Blesk, dates approximate)")
        fetched = fetch_archive(target, target)
        day_articles = fetched
    else:
        fetched = fetch_all(week_only=False)
        day_articles = [
            a for a in fetched
            if a.published is not None and local_date(a.published) == target
        ]
    print(f"  {len(fetched)} fetched, {len(day_articles)} published on {target}")

    if not day_articles:
        print("  Nothing to score. News sitemaps reach back only ~2 days; for an "
              "older date try --archive.")
        return

    budget = _budget(args.budget)

    if args.archive:
        # Archive entries have no headline, and selection ranks headlines — so
        # here the pages must be fetched first, for every article of the day.
        print(f"Reading titles and summaries from {len(day_articles)} pages...")
        enriched = enrich(day_articles)
        day_articles = [a for a in day_articles if a.title]
        print(f"  enriched {enriched}, {len(day_articles)} with a usable title")
        deduped = dedupe(day_articles)
        candidates = select(deduped, max_per_category=budget)
    else:
        deduped = dedupe(day_articles)
        candidates = select(deduped, max_per_category=budget)
        # Summaries are fetched only for the candidates, not all ~600 articles
        # of the day: one page request each, and the LLM never sees the rest.
        print("Backfilling summaries from og:description...")
        print(f"  enriched {enrich(candidates)}/{len(candidates)}")

    if args.dump:
        payload = [
            {
                "index": i,
                "category": a.category,
                "source": a.source,
                "section": a.section,
                "title": a.title,
                "summary": a.summary[:300],
                "url": a.url,
                "published": a.published.isoformat() if a.published else None,
            }
            for i, a in enumerate(candidates)
        ]
        out = Path(args.dump)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  Dumped {len(payload)} candidates to {out}")
        return

    if args.dry_run:
        for a in candidates:
            print(f"  [{a.category}] [{a.source}] {a.title}")
        return

    scored = analyze(candidates, min_score=DAILY_MIN_SCORE, budget=budget)
    kept = _keep_best(scored, KEEP_PER_DAY)

    facts = [
        {
            "score":    f.score,
            "fact":     f.fact,
            "reason":   f.reason,
            "title":    f.title,
            "source":   f.source,
            "category": f.category,
            "url":      f.url,
        }
        for f in kept
    ]
    stats = {
        "fetched":  len(fetched),
        "that_day": len(day_articles),
        "deduped":  len(deduped),
        "scored":   len(candidates),
        "passed":   len(scored),
        "kept":     len(kept),
    }
    path = days.save_day(target, facts, stats)

    print(f"\n{len(scored)} facts scored {DAILY_MIN_SCORE}+, kept top {len(kept)}")
    for f in kept:
        print(f"  [{f.score}/10] [{f.category}] {f.fact}")
    print(f"\nWrote {path.name} ({path.stat().st_size:,} bytes)")


def _budget(total: int | None) -> dict[str, int]:
    """Per-category article budget, keeping the 2:1 Czech-to-world supply ratio.

    Used to fit several days into one day's token quota when repairing history:
    seven days at the normal 180 would be ~273k tokens against a 100k cap.
    """
    if not total:
        return SCORE_BUDGET
    world = max(1, round(total / 3))
    return {"cz": max(1, total - world), "world": world}


def _keep_best(scored, keep_per_category: dict[str, int] | int):
    """Best facts per category, so one category cannot crowd out the other."""
    by_category: dict[str, list] = {}
    for fact in scored:
        by_category.setdefault(fact.category, []).append(fact)

    kept = []
    for category, facts in by_category.items():
        limit = (keep_per_category.get(category, 0)
                 if isinstance(keep_per_category, dict) else keep_per_category)
        facts.sort(key=lambda f: f.score, reverse=True)
        kept.extend(facts[:limit])

    kept.sort(key=lambda f: f.score, reverse=True)
    return kept


# --- weekly ------------------------------------------------------------------

def run_week(args) -> None:
    week_start, week_end = current_week_range(weeks_ago=args.weeks_ago)
    label = f"{week_start.date()} – {(week_end - datetime.timedelta(days=1)).date()}"
    print(f"Merging day files for {label}")

    facts, missing = days.load_range(week_start, week_end)
    print(f"  {len(facts)} facts from {7 - len(missing)} day file(s)")
    if missing:
        print(f"  WARNING: no day file for {', '.join(d.isoformat() for d in missing)} "
              f"— those days are missing from this week")

    if not facts:
        print("  Nothing to do. Run 'python main.py daily' first.")
        return

    merged = _dedupe_facts(facts)
    print(f"  {len(merged)} after dropping cross-day duplicates")

    if args.no_rerank:
        chosen = sorted(merged, key=lambda f: f.get("score", 0), reverse=True)[:args.top]
        for i, fact in enumerate(chosen, 1):
            fact["rank"] = i
    else:
        print("Re-ranking the week's best...")
        chosen = rank_week(merged, top_n=args.top)

    out = {
        "week_start": week_start.isoformat(),
        "week_end":   week_end.isoformat(),
        "generated":  datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "candidates": len(merged),
        "facts":      chosen,
    }

    key = days.week_key(week_start.date())
    path = Path(args.save) if args.save else FACTS_DIR / f"{key}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_week(chosen, label)
    print(f"Saved {len(chosen)} facts to {path}")


def _dedupe_facts(facts: list[dict]) -> list[dict]:
    """Drop the same story appearing in more than one day file.

    Day files are produced independently, so a story that broke late one
    evening can be scored again the next morning from a different outlet.
    """
    from fetcher import Article

    best_first = sorted(facts, key=lambda f: f.get("score", 0), reverse=True)

    seen_urls: set[str] = set()
    unique: list[dict] = []
    as_articles: list[Article] = []

    for fact in best_first:
        url = fact.get("url", "")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        unique.append(fact)
        # Reuse the selector's title-similarity dedupe by wrapping each fact in
        # the Article shape it expects. The fact text is compared, not the
        # headline, since that is what would appear twice in the quiz.
        as_articles.append(Article(
            source=fact.get("source", ""),
            category=fact.get("category", ""),
            title=fact.get("fact", "") or fact.get("title", ""),
            summary="",
            url=url,
        ))

    kept = {id(a) for a in dedupe(as_articles)}
    return [f for f, a in zip(unique, as_articles) if id(a) in kept]


def _print_week(facts: list[dict], label: str) -> None:
    print("\n" + "=" * 62)
    print(f"  Quiz-worthy facts  ({label})")
    print("=" * 62)
    for category in ("cz", "world"):
        items = [f for f in facts if f.get("category") == category]
        if not items:
            continue
        print(f"\n{CATEGORY_LABEL[category]}:")
        print("-" * 40)
        for fact in items:
            score = fact.get("final_score", fact.get("score", 0))
            print(f"  #{fact.get('rank', '?'):<3} [{score}/10]  {fact.get('fact', '')}")
            note = fact.get("why") or fact.get("reason", "")
            print(f"           ({fact.get('source', '')}{' — ' + note if note else ''})")
    print("\n" + "=" * 62)
    print(f"  Total: {len(facts)} facts")
    print("=" * 62 + "\n")


# --- prune -------------------------------------------------------------------

def run_prune(args) -> None:
    """Delete day files whose week has already been rendered and is old enough.

    Day files are the only archive of the ~140 weekly candidates behind each
    rendered top-20, and a week cannot be rebuilt once they are gone — sitemaps
    reach back ~2 days and no articles are stored. So a day file is removed only
    when its week has a facts/ file to show for it.
    """
    today = datetime.datetime.now(LOCAL_TZ).date()
    cutoff = today - datetime.timedelta(weeks=args.keep_weeks)

    rendered = {path.stem for path in FACTS_DIR.glob("*.json")} if FACTS_DIR.exists() else set()

    entries = days.list_days()
    if not entries:
        print("No day files to prune.")
        return

    doomed, unrendered, recent = [], [], []
    for day, path in entries:
        if day >= cutoff:
            recent.append(day)
        elif days.week_key(day) in rendered or args.force:
            doomed.append((day, path))
        else:
            unrendered.append((day, days.week_key(day)))

    print(f"days/: {len(entries)} files | cutoff {cutoff} (keeping {args.keep_weeks} weeks)")
    print(f"  {len(recent)} too recent to touch")
    if unrendered:
        print(f"  {len(unrendered)} old but their week was never rendered — kept:")
        for day, key in unrendered:
            print(f"      {day}  (no facts/{key}.json)  use --force to delete anyway")
    if not doomed:
        print("  nothing to delete")
        return

    freed = sum(path.stat().st_size for _, path in doomed)
    verb = "Would delete" if args.dry_run else "Deleting"
    print(f"  {verb} {len(doomed)} file(s), {freed:,} bytes:")
    for day, path in doomed:
        key = days.week_key(day)
        mark = f"{key} rendered" if key in rendered else f"{key} NOT rendered, forced"
        print(f"      {day}  ({mark})")

    if args.dry_run:
        print()
        print("  Dry run — nothing was deleted.")
        return

    for _, path in doomed:
        path.unlink()
    print()
    print(f"Deleted {len(doomed)} day file(s), freed {freed:,} bytes.")


# --- stats -------------------------------------------------------------------

def run_stats(args) -> None:
    s = days.stats()
    if not s["days"]:
        print("No day files yet. Run 'python main.py daily'.")
        return
    print(f"days/: {s['days']} files, {s['facts']} facts, {s['bytes']:,} bytes")
    print(f"       {s['oldest']} → {s['newest']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Quiz-worthy news fact extractor")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("daily", help="score one day and write days/<date>.json")
    d.add_argument("--date",    type=str, default=None, help="YYYY-MM-DD (default: yesterday)")
    d.add_argument("--dry-run", action="store_true",    help="fetch and select only, no LLM")
    d.add_argument("--force",   action="store_true",    help="overwrite an existing day file")
    d.add_argument("--archive", action="store_true",    help="use archive sitemaps for a day the news sitemaps lost")
    d.add_argument("--budget",  type=int, default=None, help="total articles to score (default 180; split 2:1 cz/world)")
    d.add_argument("--dump",     type=str, default=None, help="write the selected candidates to JSON and stop, no LLM")
    d.set_defaults(func=run_daily)

    w = sub.add_parser("week", help="merge day files, re-rank, write facts/")
    w.add_argument("--weeks-ago", type=int, default=1,  help="1=last week (default), 0=this week")
    w.add_argument("--top",       type=int, default=WEEKLY_TOP, help="facts to keep")
    w.add_argument("--no-rerank", action="store_true",  help="order by daily score, no LLM call")
    w.add_argument("--save",      type=str, default=None, help="write to this path instead")
    w.set_defaults(func=run_week)

    p = sub.add_parser("prune", help="delete old day files whose week was rendered")
    p.add_argument("--keep-weeks", type=int, default=8,  help="weeks of day files to keep (default: 8)")
    p.add_argument("--dry-run",    action="store_true",  help="list what would go, delete nothing")
    p.add_argument("--force",      action="store_true",  help="also delete weeks that were never rendered")
    p.set_defaults(func=run_prune)

    s = sub.add_parser("stats", help="show what is in days/")
    s.set_defaults(func=run_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
