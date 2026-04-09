"""RSS feed fetcher — pulls headlines + summaries from configured sources."""

import re
import feedparser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

FEEDS = {
    "cz": [
        ("ČT24",         "https://ct24.ceskatelevize.cz/rss/svet"),
        ("iROZHLAS",     "https://www.irozhlas.cz/rss/irozhlas"),
        ("Seznam Zprávy","https://www.seznamzpravy.cz/rss"),
        ("Novinky.cz",   "https://www.novinky.cz/rss"),
    ],
    "world": [
        ("BBC News",     "http://feeds.bbci.co.uk/news/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/world/rss"),
    ],
}

MAX_ARTICLES_PER_FEED = 20  # fetch more so date filter has enough to work with


@dataclass
class Article:
    source: str
    category: str   # "cz" or "world"
    title: str
    summary: str
    url: str = ""
    published: datetime | None = None


def current_week_range(weeks_ago: int = 1) -> tuple[datetime, datetime]:
    """Return (sunday_start, sunday_end) for a quiz week.

    weeks_ago=1 (default) → last week  (Mon–Sun before today)
    weeks_ago=0           → this week  (Sun–Sun containing today)
    """
    now = datetime.now(timezone.utc)
    days_since_sunday = (now.weekday() + 1) % 7
    this_sunday = (now - timedelta(days=days_since_sunday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start = this_sunday - timedelta(weeks=weeks_ago)
    week_end   = week_start + timedelta(days=7)
    return week_start, week_end


def fetch_all(
    max_per_feed: int = MAX_ARTICLES_PER_FEED,
    week_only: bool = True,
    weeks_ago: int = 1,
) -> list[Article]:
    week_start, week_end = current_week_range(weeks_ago=weeks_ago)
    articles: list[Article] = []

    for category, feeds in FEEDS.items():
        for source_name, feed_url in feeds:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:max_per_feed]:
                    title   = entry.get("title", "").strip()
                    summary = entry.get("summary", entry.get("description", "")).strip()
                    link    = entry.get("link", "")
                    summary = _strip_tags(summary)[:400]

                    published = _parse_date(entry)

                    if week_only and published is not None:
                        if not (week_start <= published < week_end):
                            continue

                    if title:
                        articles.append(Article(
                            source=source_name,
                            category=category,
                            title=title,
                            summary=summary,
                            url=link,
                            published=published,
                        ))
            except Exception as exc:
                print(f"[fetcher] Warning: could not fetch {source_name}: {exc}")

    return articles


def _parse_date(entry) -> datetime | None:
    """Try to extract a timezone-aware datetime from an RSS entry."""
    import time as _time
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(field)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()
