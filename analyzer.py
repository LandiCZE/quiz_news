"""
LLM-powered fact extraction + quiz-worthiness scoring.

Sends articles to Groq (free tier) in one batched prompt.
Returns a list of ScoredFact objects sorted by score descending.
"""

import json
import os
import re
import time
from dataclasses import dataclass

from groq import Groq, RateLimitError

from fetcher import Article
from selector import select

# llama-3.3-70b-versatile is no longer served on Groq. Of what remains, qwen3.8
# is the right fit: it answers in Czech, stays concise (~1.7k completion tokens
# for a 20-article batch) and returns clean JSON. The gpt-oss models spend ~5x
# more completion tokens per article, overrun max_tokens mid-array and lose the
# whole batch to truncated JSON.
MODEL = "qwen/qwen3.8-27b"

# Only facts scoring at or above this threshold appear in the final output
MIN_SCORE = 6

SYSTEM_PROMPT = """\
You are a writer for Hospodský kvíz — a popular Czech weekly pub quiz. Your job is to read news articles and identify facts that would make great quiz questions in rounds 1–2 (the "news round").

The quiz audience is Czech, aged 25–45, educated, curious, with a good sense of humor.

WHAT SCORES HIGH (8–10):
- A specific person did something surprising or funny ("rapper became PM of Nepal")
- Celebrity + unexpected connection ("Gwyneth Paltrow mentioned Robert Fico")
- Shocking/weird story with a one-word answer ("doctors found artillery shell in patient")
- Czech political figure in a notable moment ("MP arrived to parliament in folk costume")
- Record-breaking event with a specific name ("Rembrandt painting sold for record price")
- Major company merger or appointment with specific names
- Milestone with a number ("first time in 24 years", "after 33 years")
- Viral animal story ("macaque named Punch escaped with his stuffed toy")

WHAT SCORES MEDIUM (5–7):
- Solid news but answer is a country or vague term
- Sports result that's notable but not unexpected
- Business news without a surprising twist

WHAT SCORES LOW (1–4):
- Ongoing conflicts or negotiations without a specific new development
- Opinion pieces or analysis
- Economic data without a surprising number
- "Talks continue", "officials said", "sources claim"

REAL EXAMPLES OF 9–10 SCORING FACTS (for reference):
- "Val Kilmer regained his voice via AI technology" → answer: Val Kilmer
- "Doctors found an artillery shell inside a patient" → answer: dělostřelecký granát
- "A baby macaque named Punch went viral for carrying a stuffed toy everywhere" → answer: plyšáka
- "American influencer went viral for looking exactly like Slovak PM Robert Fico" → answer: Robert Fico
- "Czech MP attended a parliamentary vote dressed in folk costume" → answer: v lidovém kroji
- "Paramount/Skydance and Warner Bros./Discovery announced mergers on the same day" → answer: Paramount, Warner Bros.

You will receive a JSON array of articles. For each article, extract ONE crisp fact (max 20 words) and rate its pub-quiz worthiness 1–10.

Respond with ONLY a JSON array. Each element must have exactly these fields:
{
  "article_index": <integer>,
  "fact": "<one sentence, max 20 words, written as a quiz fact not a headline>",
  "score": <integer 1-10>,
  "reason": "<one short phrase: what makes it good or bad>"
}

Write every "fact" and "reason" in Czech. The quiz is Czech and the facts are
read out in Czech. Keep names, places and titles in their usual Czech form.

Do not include any text outside the JSON array.
"""


@dataclass
class ScoredFact:
    source: str
    category: str
    fact: str
    score: int
    reason: str
    url: str = ""
    title: str = ""   # original headline, kept so day files stay traceable


# --- Groq free-tier budget ---------------------------------------------------
# Free tier for qwen/qwen3.8-27b: 30 RPM, 1000 RPD, 8k TPM, 200k TPD — and one
# limit the published tables do not mention: OTPM, output tokens per minute,
# capped at 1000. It is enforced per request against max_tokens, and a request
# over it is rejected outright:
#
#   Request too large ... on output tokens per minute (OTPM): Limit 1000,
#   Requested 1500. ... reduce max_tokens ... and try again
#
# That is a permanent rejection, not throttling — retrying the same request
# never succeeds. It is why MAX_TOKENS must stay at or below 1000, and it sets
# the batch size: a 20-article batch measured 1,738 completion tokens, nearly
# double the cap, so batches are 10.
#
# Measured against the live API per 10-article batch:
#   prompt (system + payload)  ~2,140 tokens
#   completion                 ~  870 tokens
#   -> ~3,010 total per call
#
# OTPM, not TPM or TPD, is the binding constraint: ~870 output tokens per call
# against 1000 per minute allows roughly one call a minute, hence the 60s gap.
# 180 articles is then 18 calls, ~18 minutes and ~54k tokens — 27% of a day's
# TPD. The caps reset daily, which is the point of scoring daily instead of
# weekly: a week gets ~1,260 articles through the LLM rather than 180.
BATCH_SIZE     = 10   # articles per API call — 20 overruns the 1000 OTPM cap
BATCH_SLEEP    = 60   # seconds between batches — paces output under 1000/min
MAX_TOKENS     = 1000 # the OTPM ceiling. Above it the request is rejected
                      # outright; below ~900 a full batch risks truncation.
MAX_RETRIES    = 4

# Articles sent to the LLM per day, per category. Split 2:1 rather than evenly
# because supply is: a closed day yields ~400 Czech articles against ~120 from
# the foreign desks, and the historical output ratio was ~2.7:1 as well.
SCORE_BUDGET   = {"cz": 120, "world": 60}

# What survives into the day file. qwen scores more conservatively than the old
# model did — a strong fact lands at 7-8 rather than 9-10 — so the threshold is
# 6 and the weekly re-rank does the real head-to-head comparison.
#
# Kept per category, not 20 overall: Czech articles outnumber world ones 2:1 in
# the budget above, so a plain top-20 by score can return 20 Czech facts and no
# world ones, and then the whole week has no world round.
DAILY_MIN_SCORE = 6
KEEP_PER_DAY    = {"cz": 13, "world": 7}


def analyze(
    articles: list[Article],
    min_score: int = MIN_SCORE,
    budget: dict[str, int] | int = SCORE_BUDGET,
) -> list[ScoredFact]:
    if not articles:
        return []

    articles = select(articles, max_per_category=budget)
    print(f"  {len(articles)} articles after dedup + selection")

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    facts: list[ScoredFact] = []

    batches = [articles[i : i + BATCH_SIZE] for i in range(0, len(articles), BATCH_SIZE)]
    for i, batch in enumerate(batches):
        if i > 0:
            time.sleep(BATCH_SLEEP)
        print(f"  Batch {i + 1}/{len(batches)} ({len(batch)} articles)...")
        batch_facts = _analyze_batch(client, batch, min_score)
        facts.extend(batch_facts)

    facts.sort(key=lambda f: f.score, reverse=True)
    return facts


MAX_WAIT = 120   # seconds; a longer demand means the daily cap, not throttling


def _wait_from_error(msg: str) -> float:
    """How long to wait before retrying a Groq rate-limit error. 0 = never.

    Three cases, and only one is worth waiting for:
      * "Request too large ... reduce max_tokens" — the request exceeds a
        per-request ceiling such as OTPM. Retrying it unchanged can never
        succeed, so do not wait at all.
      * "try again in 13.085s" — an ordinary per-minute throttle, clears in
        seconds.
      * a wait longer than MAX_WAIT — the daily cap is gone; sleeping for it
        would hang the run for hours. Days are separate files, so skipping
        costs only the current day.
    """
    if "too large" in msg.lower() or "reduce max_tokens" in msg.lower():
        return 0.0

    match = re.search(r"try again in ([\d.]+)s", msg)
    wait = float(match.group(1)) + 2 if match else 60.0
    return 0.0 if wait > MAX_WAIT else wait


def _parse_json_array(raw: str) -> list[dict]:
    """Pull a JSON array out of a model response, or return [] if it cannot."""
    raw = (raw or "").strip()

    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    # Remove control characters that are invalid inside JSON strings
    # (keeps tab, newline and carriage return, which are valid JSON whitespace)
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"  JSON parse error: {exc}")
        return []

    return parsed if isinstance(parsed, list) else []


def _analyze_batch(client: Groq, articles: list[Article], min_score: int) -> list[ScoredFact]:
    payload = [
        {
            "index": i,
            "source": a.source,
            "title": a.title,
            "summary": a.summary[:200],
        }
        for i, a in enumerate(articles)
    ]

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": json.dumps(payload, ensure_ascii=False)},
                ],
            )
            break
        except RateLimitError as e:
            wait = _wait_from_error(str(e))
            if wait == 0:
                print(f"  Not retryable — {str(e)[:150]}")
                return []
            print(f"  Rate limit — waiting {wait:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
    else:
        print("  Skipping batch after too many rate limit errors")
        return []

    # A response cut off at max_tokens ends mid-array, so the JSON never parses
    # and the whole batch is lost. Say so explicitly — the symptom otherwise
    # looks like a model that cannot follow the format.
    if response.choices[0].finish_reason == "length":
        print(f"  Response hit max_tokens ({MAX_TOKENS}) and was truncated — "
              f"batch lost. Raise MAX_TOKENS or lower BATCH_SIZE.")
        return []

    results = _parse_json_array(response.choices[0].message.content)
    if not results:
        print("  Unusable JSON — skipping batch")
        return []

    facts: list[ScoredFact] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        # The model occasionally returns an out-of-range index or omits a field.
        # Skip that single item rather than letting it raise — unattended in CI
        # an exception here would lose every remaining batch of the day.
        idx   = _as_int(item.get("article_index"), -1)
        score = _as_int(item.get("score"), 0)
        if not (0 <= idx < len(articles)) or not item.get("fact"):
            continue
        if score < min_score:
            continue
        article = articles[idx]
        facts.append(ScoredFact(
            source=article.source,
            category=article.category,
            fact=item["fact"],
            score=score,
            reason=item.get("reason", ""),
            url=article.url,
            title=article.title,
        ))
    return facts


# --- weekly head-to-head re-rank ---------------------------------------------
# Daily scores are judgements made in isolation: an 8 on a quiet Tuesday and an
# 8 on a busy Saturday are not the same thing, and the model never saw the two
# side by side. The weekly pass puts the best candidates in one prompt so they
# compete directly, and drops stories that three outlets reported differently
# enough to survive title dedupe.

WEEKLY_SHORTLIST = 60   # facts sent to the re-rank — one call, ~4k tokens
WEEKLY_TOP       = 20   # facts kept for the quiz

RANK_PROMPT = """\
You are the editor of the news round for Hospodský kvíz, a Czech weekly pub quiz.

You will receive a JSON array of candidate facts from the past week. Each was
scored on its own, without seeing the others. Your job is to choose the best
ones for the round, now that you can compare them directly.

Pick the {top_n} best and rank them 1 (best) to {top_n}.

RANK HIGHER:
- a specific person, place or number that makes a crisp one-word answer
- surprising, funny, or memorable; the kind of thing people retell
- a story a Czech quiz audience plausibly noticed last week

RANK LOWER or DROP ENTIRELY:
- two candidates describing the same event — keep only the best-worded one
- vague answers ("a country", "a politician"), ongoing conflicts, routine results
- anything that reads like analysis, opinion, or a quote

Aim for variety: do not fill the round with sport or with deaths. Keep a mix of
Czech and world items roughly in proportion to how many you were given.

Write every "why" in Czech.

Respond with ONLY a JSON array, ordered best first. Each element must have
exactly these fields:
{{
  "index": <integer, the candidate's index>,
  "rank": <integer 1-{top_n}>,
  "final_score": <integer 1-10>,
  "why": "<one short phrase>"
}}

Do not include any text outside the JSON array.
"""


def _by_daily_score(ordered: list[dict], top_n: int) -> list[dict]:
    """Fallback ordering when the re-rank call cannot be used.

    Still fills in rank and final_score: downstream renders "#{rank}" and a
    badge, and a bare fact list would publish a page full of "#None".
    """
    chosen = []
    for position, fact in enumerate(ordered[:top_n], 1):
        chosen.append({
            **fact,
            "rank": position,
            "final_score": fact.get("score", 0),
            "why": fact.get("reason", ""),
        })
    return chosen


def rank_week(
    facts: list[dict],
    top_n: int = WEEKLY_TOP,
    shortlist: int = WEEKLY_SHORTLIST,
) -> list[dict]:
    """Re-rank a week of daily facts in one prompt. Returns the chosen facts.

    Falls back to the daily scores if the call or its JSON cannot be used, so a
    bad response degrades the ordering rather than losing the week.
    """
    if not facts:
        return []

    ordered = sorted(facts, key=lambda f: f.get("score", 0), reverse=True)
    candidates = ordered[:shortlist]

    payload = [
        {
            "index": i,
            "fact": f.get("fact", ""),
            "category": f.get("category", ""),
            "source": f.get("source", ""),
            "daily_score": f.get("score", 0),
        }
        for i, f in enumerate(candidates)
    ]

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    print(f"  Re-ranking {len(candidates)} candidates in one call...")

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[
                    {"role": "system", "content": RANK_PROMPT.format(top_n=top_n)},
                    {"role": "user",   "content": json.dumps(payload, ensure_ascii=False)},
                ],
            )
            break
        except RateLimitError as e:
            wait = _wait_from_error(str(e))
            if wait == 0:
                print(f"  Not retryable — falling back to daily scores. {str(e)[:120]}")
                return _by_daily_score(ordered, top_n)
            print(f"  Rate limit — waiting {wait:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
    else:
        print("  Re-rank failed — falling back to daily scores")
        return _by_daily_score(ordered, top_n)

    if response.choices[0].finish_reason == "length":
        print(f"  Re-rank truncated at max_tokens ({MAX_TOKENS}) — "
              f"falling back to daily scores")
        return _by_daily_score(ordered, top_n)

    results = _parse_json_array(response.choices[0].message.content)
    if not results:
        print("  Re-rank returned unusable JSON — falling back to daily scores")
        return _by_daily_score(ordered, top_n)

    chosen: list[dict] = []
    seen: set[int] = set()
    for item in sorted(results, key=lambda r: _as_int(r.get("rank"), 999)):
        idx = _as_int(item.get("index"), -1)
        if not (0 <= idx < len(candidates)) or idx in seen:
            continue
        seen.add(idx)
        chosen.append({
            **candidates[idx],
            "rank": len(chosen) + 1,
            "final_score": _as_int(item.get("final_score"), candidates[idx].get("score", 0)),
            "why": item.get("why", ""),
        })

    if not chosen:
        print("  Re-rank matched no candidates — falling back to daily scores")
        return _by_daily_score(ordered, top_n)

    print(f"  Kept {len(chosen)} of {len(candidates)} candidates")
    return chosen[:top_n]


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
