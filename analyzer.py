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

from groq import Groq

from fetcher import Article

# Free on Groq — fast and capable enough for this task
MODEL = "llama-3.3-70b-versatile"

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


BATCH_SIZE  = 20   # articles per API call
BATCH_SLEEP = 30   # seconds between batches — conservative for 12k TPM free tier
MAX_RETRIES = 4


def _dedup(articles: list[Article]) -> list[Article]:
    """Drop articles with near-duplicate titles (same first 60 chars)."""
    seen: set[str] = set()
    out: list[Article] = []
    for a in articles:
        key = a.title[:60].lower().strip()
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def analyze(articles: list[Article], min_score: int = MIN_SCORE) -> list[ScoredFact]:
    if not articles:
        return []

    articles = _dedup(articles)
    print(f"  {len(articles)} articles after dedup")

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


def _wait_from_error(msg: str) -> float:
    """Parse 'Please try again in 13.085s' from Groq error message."""
    m = re.search(r"try again in ([\d.]+)s", msg)
    return float(m.group(1)) + 2 if m else 60.0


def _analyze_batch(client: Groq, articles: list[Article], min_score: int) -> list[ScoredFact]:
    from groq import RateLimitError

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
                max_tokens=4096,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": json.dumps(payload, ensure_ascii=False)},
                ],
            )
            break
        except RateLimitError as e:
            wait = _wait_from_error(str(e))
            print(f"  Rate limit — waiting {wait:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
    else:
        print("  Skipping batch after too many rate limit errors")
        return []

    raw = response.choices[0].message.content.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    results = json.loads(raw)

    facts: list[ScoredFact] = []
    for item in results:
        idx   = item["article_index"]
        score = int(item["score"])
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
        ))
    return facts
