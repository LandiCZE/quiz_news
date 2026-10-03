"""Backfill for days the news sitemaps no longer reach.

`sitemap_news.xml` holds only the last ~2 days, so a day older than that is
normally unrecoverable. Four outlets also publish a dated *archive* sitemap
going back months, which is enough to reconstruct an older day — with three
compromises the caller should know about:

  * Only Novinky, Seznam Zprávy, Sport.cz and ČT24 expose a usable archive.
    iROZHLAS and Blesk offer nested indexes without dates, iDNES and
    Aktuálně.cz do not go back far enough. Expect ~45% of a normal day's
    volume, and no Blesk means no celebrity/viral slice.
  * Archive entries carry no headline, so the title is read from each page's
    og:title by enrich.py. That is one HTTP request per candidate article.
  * They carry `lastmod`, not publication time. For an article nobody edited
    the two match; for one revised later the date can be off by a day.

Use it only to repair history. The daily job never needs it.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date

import fetcher
from fetcher import Article, Source

# Archive sitemap indexes. Children are ordered newest-first, so the walk below
# stops as soon as it has covered the requested dates.
ARCHIVE_INDEXES = {
    "Novinky.cz":    "https://www.novinky.cz/sitemaps/sitemap_articles.xml",
    "Seznam Zprávy": "https://www.seznamzpravy.cz/sitemaps/sitemap_articles.xml",
    "Sport.cz":      "https://www.sport.cz/sitemaps/sitemap_articles.xml",
    "ČT24":          "https://ct24.ceskatelevize.cz/sitemaps/sitemap_articles.xml",
}

MAX_CHUNKS = 3   # per source; one chunk is ~10k URLs and already spans months


def fetch_archive(start: Date, end: Date) -> list[Article]:
    """Articles published between start and end inclusive, titles left empty."""
    sources = [s for s in fetcher.SOURCES if s.name in ARCHIVE_INDEXES]

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        results = pool.map(lambda s: _fetch_one(s, start, end), sources)

    articles: list[Article] = []
    for source, found in zip(sources, results):
        print(f"[archive] {source.name}: {len(found)} articles")
        articles.extend(found)
    return articles


def _fetch_one(source: Source, start: Date, end: Date) -> list[Article]:
    index_url = ARCHIVE_INDEXES[source.name]
    try:
        index = fetcher._parse_xml(_get(index_url))
        children = fetcher._children_sitemaps(index)
    except Exception as exc:
        print(f"[archive] Warning: {source.name} index failed: {exc}")
        return []

    if not children:
        children = [index_url]

    articles: list[Article] = []
    seen: set[str] = set()

    for child in children[:MAX_CHUNKS]:
        try:
            entries = fetcher._urlset_entries(fetcher._parse_xml(_get(child)))
        except Exception as exc:
            print(f"[archive] Warning: {source.name} chunk failed: {exc}")
            continue

        dates = [e["published"] for e in entries if e["published"]]
        for entry in entries:
            article = _to_article(source, entry, start, end, seen)
            if article is not None:
                articles.append(article)

        # Children run newest-first, so once a chunk reaches back past `start`
        # every remaining chunk is older still.
        if dates and min(fetcher.local_date(d) for d in dates) <= start:
            break

    return articles


def _to_article(source: Source, entry: dict, start: Date, end: Date,
                seen: set[str]) -> Article | None:
    url = entry["url"]
    published = entry["published"]
    if not url or published is None or url in seen:
        return None

    day = fetcher.local_date(published)
    if not (start <= day <= end):
        return None

    section = source.section_of(url) if source.section_of else ""
    tokens = fetcher._section_tokens(section)
    if tokens & fetcher.SKIP_SECTIONS:
        return None

    seen.add(url)
    return Article(
        source=source.name,
        category="world" if tokens & fetcher.WORLD_SECTIONS else source.default_category,
        title="",            # archive sitemaps carry none; enrich.py reads og:title
        summary="",
        url=url,
        published=published,
        section=section,
    )


def _get(url: str) -> bytes:
    import requests
    response = requests.get(url, headers={"User-Agent": fetcher.USER_AGENT}, timeout=60)
    response.raise_for_status()
    return response.content
