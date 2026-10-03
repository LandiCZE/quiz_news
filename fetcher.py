"""News-sitemap fetcher — pulls headlines from Google News sitemaps.

Replaces the old RSS fetcher. RSS section feeds only expose ~20 of the newest
items from one section (e.g. ct24/rss/svet was world news only), which is why
most quiz-worthy stories never showed up. Every Czech outlet also publishes a
`sitemap_news.xml` for Google News: the full set of articles from the last ~2
days, across *all* sections, with exact publication timestamps and titles.

Typical yield per run: ~1200 articles vs ~120 from RSS.

Sitemaps carry no summary text, so `enrich.py` backfills it from each page's
og:description. Section is derived from the URL and used to split cz/world and
to drop low-value rubrics.
"""

import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

USER_AGENT = "Mozilla/5.0 (compatible; quiz_news/1.0; +https://github.com/)"
TIMEOUT = 25
MAX_WORKERS = 8
MAX_INDEX_CHILDREN = 4  # how many child sitemaps to follow from an index


# --- section extraction strategies -------------------------------------------
# Each outlet encodes the rubric differently, so the section is pulled out
# per-source rather than guessed from the whole URL (a title slug containing
# "svet" must not turn a domestic story into world news).

def _slug_section(url: str, after: str = "clanek") -> str:
    """Seznam-family CMS: /clanek/<section>-<rest-of-title-slug>-<id>"""
    parts = _path_parts(url)
    if after in parts:
        i = parts.index(after)
        if i + 1 < len(parts):
            return parts[i + 1].split("-")[0]
    return ""


def _segment_section(url: str, index: int) -> str:
    """Section lives in its own path segment, e.g. ct24 /clanek/<svet>/<slug>"""
    parts = _path_parts(url)
    return parts[index] if index < len(parts) else ""


def _idnes_section(url: str) -> str:
    """iDNES: /<site>/<section>/<slug> — both segments carry meaning."""
    parts = _path_parts(url)
    return " ".join(parts[:2])


def _section_tokens(section: str) -> set[str]:
    """Split a section into matchable tokens, keeping the whole string too.

    Outlets build compound sections with hyphens — Blesk files under
    "regiony-praha-praha-zpravy" and iROZHLAS under "zpravy-svet" — so matching
    on whitespace alone would never see the "regiony" or "svet" inside them.
    """
    return {t for t in re.split(r"[\s-]+", section) if t} | {section}


def _path_parts(url: str) -> list[str]:
    return [p for p in urlparse(url).path.lower().strip("/").split("/") if p]


@dataclass
class Source:
    name: str
    sitemaps: list[str]
    section_of: object = None          # callable(url) -> section string
    default_category: str = "cz"      # used when the section says nothing


SOURCES = [
    Source("Novinky.cz",     ["https://www.novinky.cz/sitemaps/sitemap_news.xml"],
           section_of=_slug_section),
    Source("Seznam Zprávy",  ["https://www.seznamzpravy.cz/sitemaps/sitemap_news.xml"],
           section_of=_slug_section),
    Source("Sport.cz",       ["https://www.sport.cz/sitemaps/sitemap_news.xml"],
           section_of=_slug_section),
    Source("ČT24",           ["https://ct24.ceskatelevize.cz/sitemaps/sitemap_news.xml"],
           section_of=lambda u: _segment_section(u, 1)),
    Source("iROZHLAS",       ["https://www.irozhlas.cz/sites/default/files/irozhlas_feeds/sitemaps/news.xml"],
           section_of=lambda u: _segment_section(u, 0)),
    Source("iDNES",          ["https://www.idnes.cz/zpravy/sitemap",
                              "https://www.idnes.cz/revue/sitemap",
                              "https://www.idnes.cz/ekonomika/sitemap",
                              "https://www.idnes.cz/kultura/sitemap"],
           section_of=_idnes_section),
    Source("Blesk",          ["https://www.blesk.cz/sitemap-news.xml"],
           section_of=lambda u: _segment_section(u, 1)),
    Source("Aktuálně.cz",    ["https://www.aktualne.cz/sitemapnews/"],
           section_of=lambda u: _segment_section(u, 0)),
]
# All sources are Czech outlets. World news comes from their foreign desks
# (the "zahraniční"/"svět" sections), which is what a Czech quiz audience
# actually saw that week — and the bizarre stories that make the best
# questions get carried by Novinky and iDNES anyway. Foreign-language wires
# (BBC, Guardian) were dropped: they buried the budget under UK regional
# filler like "New free repair shop in Jericho area of Oxford".

# Section tokens that mark an article as world news on an otherwise Czech site.
WORLD_SECTIONS = {
    "zahranicni",    # Novinky, Seznam Zprávy, iDNES ("zpravy zahranicni")
    "zahranici",     # Aktuálně.cz
    "svet",          # ČT24
    "zpravy-svet",   # iROZHLAS, Blesk
    "valka",         # Novinky's Ukraine desk
}

# Rubrics that reliably produce nothing quiz-worthy — dropped before the LLM
# ever sees them (the scoring prompt rates opinion and regional filler 1–4).
SKIP_SECTIONS = {
    "komentare", "nazory", "komentar", "audio", "podcast", "podcasty",
    "regiony", "regionalni", "bydleni", "recepty", "horoskopy", "inzerce",
    "program", "tv-program", "soutez", "komercni", "pr-clanek", "advertorial",
    "info", "firmy-v-cr", "hrytydenik", "profil", "temata", "tema",
}

MAX_PER_SOURCE = 400  # safety valve against a sitemap index blowing up

# Czech news days, not UTC days: a story filed at 01:00 Prague belongs to that
# day's news, but in UTC it would fall into the day before.
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Prague")
except Exception:                                    # pragma: no cover
    LOCAL_TZ = timezone.utc


def local_date(moment: datetime):
    return moment.astimezone(LOCAL_TZ).date()

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class Article:
    source: str
    category: str   # "cz" or "world"
    title: str
    summary: str
    url: str = ""
    published: datetime | None = None
    section: str = ""


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
    week_only: bool = True,
    weeks_ago: int = 1,
    sources: list[Source] | None = None,
) -> list[Article]:
    week_start, week_end = current_week_range(weeks_ago=weeks_ago)
    sources = sources if sources is not None else SOURCES

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(_fetch_source, sources))

    articles: list[Article] = []
    for source, source_articles in zip(sources, results):
        kept = [
            a for a in source_articles
            if not (week_only and a.published is not None
                    and not (week_start <= a.published < week_end))
        ]
        print(f"[fetcher] {source.name}: {len(kept)} articles")
        articles.extend(kept)

    return articles


def _fetch_source(source: Source) -> list[Article]:
    articles: list[Article] = []
    seen: set[str] = set()

    for sitemap_url in source.sitemaps:
        try:
            entries = _read_sitemap(sitemap_url)
        except Exception as exc:
            print(f"[fetcher] Warning: could not fetch {source.name} ({sitemap_url}): {exc}")
            continue

        for entry in entries:
            url = entry["url"]
            if url in seen:
                continue
            seen.add(url)

            section = source.section_of(url) if source.section_of else ""
            tokens = _section_tokens(section)
            if tokens & SKIP_SECTIONS:
                continue

            category = "world" if tokens & WORLD_SECTIONS else source.default_category

            title = entry["title"] or _title_from_slug(url)
            if not title:
                continue

            articles.append(Article(
                source=source.name,
                category=category,
                title=title,
                summary="",
                url=url,
                published=entry["published"],
                section=section,
            ))

    # A sitemap index concatenates its children, so sort before capping —
    # otherwise the cap would keep whichever child happened to come first.
    articles.sort(key=lambda a: a.published or _EPOCH, reverse=True)
    return articles[:MAX_PER_SOURCE]


def _read_sitemap(url: str, _depth: int = 0) -> list[dict]:
    """Fetch one sitemap. Follows a <sitemapindex> one level down."""
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    response.raise_for_status()

    root = _parse_xml(response.content)

    children = _children_sitemaps(root)
    if children and _depth == 0:
        entries: list[dict] = []
        for child in children[:MAX_INDEX_CHILDREN]:
            try:
                entries.extend(_read_sitemap(child, _depth=1))
            except Exception as exc:
                print(f"[fetcher] Warning: child sitemap {child} failed: {exc}")
        return entries

    return _urlset_entries(root)


def _parse_xml(content: bytes):
    """Parse sitemap XML, working around outlets that lie about their encoding.

    Bytes are passed in rather than text so the XML prolog decides the codec —
    except iDNES, which declares windows-1250 while actually serving UTF-8.
    """
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        pass

    retagged = re.sub(rb'encoding=["\'][^"\']+["\']', b'encoding="utf-8"', content, count=1)
    try:
        return ET.fromstring(retagged)
    except ET.ParseError:
        # Give up on the declaration entirely and decode leniently.
        text = content.decode("utf-8", errors="replace")
        return ET.fromstring(re.sub(r"^\s*<\?xml.*?\?>", "", text, count=1).strip())


def _local(tag: str) -> str:
    """Strip the XML namespace — outlets use different prefixes (news: vs n:)."""
    return tag.rsplit("}", 1)[-1]


def _children_sitemaps(root) -> list[str]:
    out = []
    for node in root:
        if _local(node.tag) != "sitemap":
            continue
        for child in node:
            if _local(child.tag) == "loc" and child.text:
                out.append(child.text.strip())
    return out


def _urlset_entries(root) -> list[dict]:
    entries = []
    for node in root:
        if _local(node.tag) != "url":
            continue

        url = title = ""
        lastmod = published = None

        for child in node:
            name = _local(child.tag)
            text = (child.text or "").strip()
            if name == "loc":
                url = text
            elif name == "lastmod":
                lastmod = _parse_date(text)
            elif name == "news":
                for leaf in child:
                    leaf_name = _local(leaf.tag)
                    leaf_text = (leaf.text or "").strip()
                    if leaf_name == "title":
                        title = leaf_text
                    elif leaf_name == "publication_date":
                        published = _parse_date(leaf_text)

        if url:
            entries.append({
                "url": url,
                "title": title,
                "published": published or lastmod,
            })

    return entries


def _parse_date(text: str) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _title_from_slug(url: str) -> str:
    """Last-resort title for sitemaps without news metadata."""
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.(html?|A\d{6}_.*)$", "", slug)
    slug = re.sub(r"[-_]\d+$", "", slug)
    words = [w for w in re.split(r"[-_]", slug) if w and not w.isdigit()]
    return " ".join(words).capitalize() if len(words) >= 3 else ""
