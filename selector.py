"""Candidate selection — picks which articles the LLM actually scores.

Sitemaps yield ~1200 articles a day where RSS yielded ~120, so the old
"newest N per category" cap would have fed the LLM nothing but the last hour
of Sunday. Selection therefore:

  1. drops near-duplicates (the same story carried by eight outlets),
  2. ranks each headline with a cheap quiz-worthiness heuristic that mirrors
     the scoring prompt (specific names, numbers, records, oddities),
  3. fills the per-category budget evenly across the days of the week, so
     Monday's stories still get a look on Sunday.

The heuristic only orders candidates — the LLM still does the real scoring.
"""

import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

from fetcher import Article, local_date

# Headline markers that match what the scoring prompt rewards.
GOOD_PATTERNS = [
    (3, r"\b(poprvé|naposledy|rekord\w*|nejstarš\w+|nejmladš\w+|největš\w+|nejmenš\w+|nejdražš\w+|nejrychlejš\w+|historicky prvn\w+)\b"),
    (3, r"\b(record|first ever|first time|oldest|youngest|largest|biggest|most expensive)\b"),
    (3, r"\b(zemřel\w*|umřel\w*|died|dies)\b"),
    (3, r"\b(viráln\w+|viral|zaujal\w*|šokoval\w*|překvapil\w*|rozesmál\w*|bizar\w+|kurióz\w+|neobvykl\w+|nezvykl\w+)\b"),
    (2, r"\b(jmenován\w*|zvolen\w*|odstoupil\w*|rezignoval\w*|vystřídal\w*|nahradil\w*|appointed|elected|resigns?|quits?)\b"),
    (2, r"\b(fúze|koupil\w*|převzal\w*|prodal\w*|merger|acquires?|buys?|takeover)\b"),
    (2, r"\b(vyhrál\w*|získal\w*|porazil\w*|wins?|beats?|defeats?)\b"),
    (2, r"\b(našli|našel|objevil\w*|ukradl\w*|unikl\w*|utekl\w*|found|discovered|stolen|escaped)\b"),
    (2, r"\b(cena|ocenění|award|oscar|nobel\w*|grammy|zlat\w+ (medaile|míč))\b"),
]

# Headline markers for the "ongoing talks / opinion / analysis" pile the
# prompt explicitly rates 1–4.
BAD_PATTERNS = [
    (-4, r"\b(komentář|glosa|analýza|rozhovor|názor|esej|recenze|podcast|anketa)\b"),
    (-4, r"\b(opinion|analysis|explainer|editorial|review|podcast|live updates?)\b"),
    (-3, r"\b(podle|tvrdí|míní|uvedl\w*|řekl\w*|says?|said|claims?|reportedly)\b"),
    (-3, r"\b(pokračuj\w+|jednání|vyjednáván\w+|debatuj\w+|talks continue|negotiations)\b"),
    (-2, r"\b(mohl\w* by|možná|údajně|zvažuje|plánuje|could|might|may|considers?|plans to)\b"),
    (-2, r"\b(jak |proč |co dělat|rady|tipy|how to|why |what to)\b"),
    (-2, r"\b(online|přímý přenos|sledujte|watch live|minuta po minutě)\b"),
]

GOOD_RULES = [(w, re.compile(p, re.I)) for w, p in GOOD_PATTERNS]
BAD_RULES  = [(w, re.compile(p, re.I)) for w, p in BAD_PATTERNS]

PROPER_NOUN = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]{2,}")
NUMBER      = re.compile(r"\b\d{1,4}([.,]\d+)?\b")
QUOTED      = re.compile(r"[\"„“»]")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Two headlines sharing this fraction of their significant words — and at
# least MIN_SHARED_TOKENS of them — are treated as the same story. Tuned by
# eyeballing every pair in the 0.38–0.62 band on a live fetch: all were real
# duplicates (the same death, flood or match reported by three outlets).
DUPLICATE_OVERLAP = 0.4
MIN_SHARED_TOKENS = 3
STOPWORDS = {
    "a", "aby", "ale", "ani", "az", "bude", "budou", "by", "byl", "byla", "bylo",
    "ci", "co", "do", "i", "je", "jeho", "jen", "ji", "jiz", "jsou", "k", "kdo",
    "kdyz", "ktera", "ktere", "ktery", "ma", "maji", "me", "mu", "na", "nad",
    "nebo", "nejsou", "neni", "o", "od", "po", "pod", "pro", "pred", "pri", "s",
    "se", "si", "tak", "take", "tim", "to", "u", "uz", "v", "ve", "with", "vsak",
    "z", "za", "ze", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "has", "have", "in", "is", "it", "its", "of", "on", "or", "that", "the",
    "they", "this", "to", "was", "were", "will", "who", "after", "over",
}


def select(
    articles: list[Article],
    max_per_category: int | dict[str, int],
    verbose: bool = True,
) -> list[Article]:
    """Rank and thin articles down to the LLM budget, balanced across days.

    `max_per_category` is either one number for every category or a per-category
    budget, since Czech and world supply differ by roughly 3:1.
    """
    if not articles:
        return []

    unique = dedupe(articles)

    by_category: dict[str, list[Article]] = defaultdict(list)
    for article in unique:
        by_category[article.category].append(article)

    chosen: list[Article] = []
    for category, items in by_category.items():
        budget = (max_per_category.get(category, 0)
                  if isinstance(max_per_category, dict) else max_per_category)
        picked = _pick_balanced(items, budget)
        if verbose:
            days = len({_day(a) for a in picked})
            print(f"  [select] {category}: {len(items)} → {len(picked)} "
                  f"across {days} day(s), {len({a.source for a in picked})} source(s)")
        chosen.extend(picked)

    return chosen


def dedupe(articles: list[Article]) -> list[Article]:
    """Drop repeats of the same story, whether or not the wording matches."""
    out: list[Article] = []
    seen_exact: set[str] = set()
    seen_tokens: list[set[str]] = []

    # Prefer the earliest report of a story, so the outlet that broke it wins.
    for article in sorted(articles, key=lambda a: a.published or _EPOCH):
        normalized = _normalize(article.title)
        if not normalized or normalized in seen_exact:
            continue

        tokens = _significant_tokens(normalized)
        if tokens and any(_is_same_story(tokens, other) for other in seen_tokens):
            continue

        seen_exact.add(normalized)
        if tokens:
            seen_tokens.append(tokens)
        out.append(article)

    return out


def score_headline(article: Article) -> int:
    """Cheap pre-LLM guess at quiz-worthiness. Higher is more promising."""
    text = f"{article.title} {article.summary[:160]}"
    score = 0

    for weight, rule in GOOD_RULES:
        if rule.search(text):
            score += weight
    for weight, rule in BAD_RULES:
        if rule.search(text):
            score += weight

    # Specific people, places and brands are what quiz answers are made of.
    score += min(len(set(PROPER_NOUN.findall(article.title))), 3) * 2
    if NUMBER.search(article.title):
        score += 2
    if QUOTED.search(article.title):
        score -= 2          # headline built around a quote = someone said something

    words = len(article.title.split())
    if words < 4 or words > 18:
        score -= 2          # too thin to extract a fact, or a rambling liveblog title
    if article.summary:
        score += 1          # the LLM has more than a headline to work with

    return score


def _pick_balanced(articles: list[Article], budget: int) -> list[Article]:
    """Spread the budget over both days and sources, best story first.

    Each (day, source) group is ranked internally, then every group's best is
    taken before any group's second. Without the source half of that, one
    outlet floods the budget: Sport.cz files ~124 of the ~400 Czech articles a
    day and match reports are exactly what the heuristic rewards, so the LLM
    was being handed a sports round instead of a news round.
    """
    scores = {id(a): score_headline(a) for a in articles}

    groups: dict[tuple[str, str], list[Article]] = defaultdict(list)
    for article in articles:
        groups[(_day(article), article.source)].append(article)

    ranked: list[tuple[int, int, Article]] = []
    for items in groups.values():
        items.sort(key=lambda a: scores[id(a)], reverse=True)
        for position, article in enumerate(items):
            ranked.append((position, -scores[id(article)], article))

    # position first, so round N of every group precedes round N+1 of any
    ranked.sort(key=lambda row: (row[0], row[1]))

    picked = [article for _, _, article in ranked[:budget]]
    picked.sort(key=lambda a: a.published or _EPOCH, reverse=True)
    return picked


def _day(article: Article) -> str:
    """The article's Czech calendar day, matching how day files are cut."""
    if article.published is None:
        return "unknown"
    return local_date(article.published).isoformat()


def _normalize(title: str) -> str:
    text = unicodedata.normalize("NFKD", title.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", text).strip()


def _significant_tokens(normalized: str) -> set[str]:
    return {w for w in normalized.split() if len(w) > 2 and w not in STOPWORDS}


def _is_same_story(a: set[str], b: set[str]) -> bool:
    shared = a & b
    return len(shared) >= MIN_SHARED_TOKENS and _overlap(a, b) >= DUPLICATE_OVERLAP


def _overlap(a: set[str], b: set[str]) -> float:
    """Share of the smaller headline's words that also appear in the other."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))
