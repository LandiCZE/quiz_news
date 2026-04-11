"""
JSON-based article storage.

Articles are kept in articles.json — a flat list deduplicated by URL.
JSON works better than SQLite in git (readable diffs, no binary blobs).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from fetcher import Article

STORE_PATH = Path(__file__).parent / "articles.json"


def _load(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


def _save(data: list[dict], path: Path) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_articles(articles: list[Article], store_path: Path = STORE_PATH) -> int:
    """Append new articles, skip duplicates by URL. Returns count of new articles."""
    existing = _load(store_path)
    seen_urls = {a["url"] for a in existing if a.get("url")}

    new_entries = []
    for a in articles:
        key = a.url or a.title
        if key in seen_urls:
            continue
        seen_urls.add(key)
        new_entries.append({
            "url":       a.url,
            "source":    a.source,
            "category":  a.category,
            "title":     a.title,
            "summary":   a.summary,
            "published": a.published.isoformat() if a.published else None,
        })

    if new_entries:
        _save(existing + new_entries, store_path)

    return len(new_entries)


def get_articles(
    week_start: datetime,
    week_end: datetime,
    store_path: Path = STORE_PATH,
) -> list[Article]:
    """Return stored articles whose published date falls in [week_start, week_end)."""
    raw = _load(store_path)
    articles = []
    for a in raw:
        pub_str = a.get("published")
        if not pub_str:
            continue
        pub = datetime.fromisoformat(pub_str).replace(tzinfo=timezone.utc)
        if week_start <= pub < week_end:
            articles.append(Article(
                source=a["source"],
                category=a["category"],
                title=a["title"],
                summary=a.get("summary", ""),
                url=a.get("url", ""),
                published=pub,
            ))
    return articles


def prune_articles(keep_from: datetime, store_path: Path = STORE_PATH) -> int:
    """Remove articles published before keep_from. Returns count removed."""
    raw = _load(store_path)
    kept = [
        a for a in raw
        if a.get("published") and
           datetime.fromisoformat(a["published"]).replace(tzinfo=timezone.utc) >= keep_from
    ]
    removed = len(raw) - len(kept)
    if removed:
        _save(kept, store_path)
    return removed


def store_stats(store_path: Path = STORE_PATH) -> dict:
    raw = _load(store_path)
    if not raw:
        return {"total": 0, "oldest": None, "newest": None}
    dates = sorted(a["published"] for a in raw if a.get("published"))
    return {
        "total":  len(raw),
        "oldest": dates[0] if dates else None,
        "newest": dates[-1] if dates else None,
    }
