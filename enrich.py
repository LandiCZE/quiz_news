"""Summary backfill — news sitemaps give titles only, no description.

RSS entries used to carry a summary, and the scoring prompt reads
`title + summary[:200]`, so without this step the LLM would judge every story
on its headline alone. Each article page is fetched once and its
og:description (or <meta name="description">) is used as the summary.

Runs concurrently and only downloads the first ~60 kB of each page, since the
meta tags live in <head>.
"""

import re
from concurrent.futures import ThreadPoolExecutor

import requests

from fetcher import Article, USER_AGENT

TIMEOUT = 15
MAX_WORKERS = 12
HEAD_BYTES = 60_000
MAX_SUMMARY = 400

def _meta_patterns(attr: str, value: str) -> list[re.Pattern]:
    """Match one <meta> tag, in either attribute order.

    Everything is kept inside the tag: `[^>]*?` between attributes and
    `[^>]*?` for the value, with the closing quote matched by backreference.
    A greedier `(.*?)` spans tags — on Aktuálně.cz it swallowed the
    Content-Type meta and prefixed every summary with 'text/html; charset=…'.
    """
    tag = rf'{attr}=["\']{value}["\']'
    content = r'content=(["\'])([^>]*?)\1'
    return [
        re.compile(rf"<meta[^>]*?{tag}[^>]*?{content}", re.I),
        re.compile(rf"<meta[^>]*?{content}[^>]*?{tag}", re.I),
    ]


# og:description first — it is the editorial teaser; name=description is often
# a truncated or SEO-stuffed variant.
_META_PATTERNS = (
    _meta_patterns("property", "og:description")
    + _meta_patterns("name", "description")
)

# Only needed for archive backfill: those sitemaps carry no headline, so the
# title has to come from the page itself.
_TITLE_PATTERNS = _meta_patterns("property", "og:title")
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&nbsp;": " ", "&hellip;": "…",
}


# Outlet names that og:title and <title> tag onto the end of a headline, e.g.
# "Francie vyhrala v Belgii - Sport.cz". Only a trailing match against this
# list is removed, so a headline that legitimately contains a dash survives.
_OUTLETS = [
    "Novinky.cz", "Novinky", "Seznam Zpravy", "Seznam Zprávy", "SeznamZprávy",
    "Sport.cz", "ČT24", "Česká televize", "iROZHLAS", "iROZHLAS.cz",
    "iDNES.cz", "iDNES", "Blesk.cz", "Blesk", "Aktuálně.cz", "Aktualne.cz",
]
_OUTLET_SUFFIX = re.compile(
    r"\s*[-|–—·]\s*(?:" + "|".join(re.escape(o) for o in _OUTLETS) + r")\s*$",
    re.I,
)


def _strip_outlet(title: str) -> str:
    for _ in range(2):          # some pages append the name twice
        stripped = _OUTLET_SUFFIX.sub("", title).strip()
        if stripped == title or not stripped:
            break
        title = stripped
    return title


def enrich(articles: list[Article], max_workers: int = MAX_WORKERS) -> int:
    """Fill in `summary` — and `title` when missing. Returns count enriched.

    A missing title means the article came from an archive sitemap, which lists
    no headline, so both are read from the same page fetch.
    """
    todo = [a for a in articles if (not a.summary or not a.title) and a.url]
    if not todo:
        return 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_fetch_meta, todo))

    enriched = 0
    for article, (title, summary) in zip(todo, results):
        if summary and not article.summary:
            article.summary = summary
        if title and not article.title:
            article.title = title
        if summary or title:
            enriched += 1

    return enriched


def _fetch_meta(article: Article) -> tuple[str, str]:
    try:
        response = requests.get(
            article.url,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
            stream=True,
        )
        response.raise_for_status()
        chunk = response.raw.read(HEAD_BYTES, decode_content=True)
        response.close()
    except Exception:
        return "", ""

    encoding = response.encoding or "utf-8"
    html = chunk.decode(encoding, errors="replace")

    summary = ""
    for pattern in _META_PATTERNS:
        match = pattern.search(html)
        if match:
            summary = _clean(match.group(2))
            break

    title = ""
    if not article.title:
        for pattern in _TITLE_PATTERNS:
            match = pattern.search(html)
            if match:
                title = _clean(match.group(2))
                break
        if not title:
            match = _TITLE_TAG.search(html)
            if match:
                title = _clean(match.group(1))
        title = _strip_outlet(title)

    return title, summary


def _clean(text: str) -> str:
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()[:MAX_SUMMARY]
