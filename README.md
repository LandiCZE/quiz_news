# quiz_news

Turns a week of Czech news into quiz-worthy facts for a pub quiz news round.

## Pipeline

Two jobs. The daily one does the work; the weekly one picks the winners.

```
daily   fetcher.py   news sitemaps      ~1200 articles, one closed Prague day
        selector.py  dedupe + rank      180 candidates (120 cz / 60 world)
        enrich.py    og:description     summaries for those 180 only
        analyzer.py  Groq LLM           scores each 1-10
        days.py      days/<date>.json   top 13 cz + 7 world, ~9 kB

weekly  days.py      load the 7 files   ~140 candidates
        main.py      cross-day dedupe   same story from two days collapses
        analyzer.py  Groq LLM           one call, head-to-head re-rank
        facts/       YYYY-Wnn.json      the week's top 20
        render.py    index.html         newest week
                     weeks/*.html       one page per week, kept forever
```

Raw articles are never stored. Each run processes one closed calendar day
selected by publication timestamp, so runs never overlap and no URL bookkeeping
is needed to avoid scoring the same article twice. A day costs ~9 kB instead of
the ~1.2 MB of articles it came from.

## Why sitemaps instead of RSS

RSS was the original source and it missed most of what the quiz actually uses.
A feed like `ct24.ceskatelevize.cz/rss/svet` is a *single section* (world news)
capped at roughly 20 of the newest items, so domestic politics, sport, culture
and the viral oddities that make the best questions never appeared at all.

Every outlet also publishes a Google News sitemap — `sitemap_news.xml` — listing
every article from the last ~2 days across all sections, with exact publication
timestamps and headlines. Same sites, no API key, ~10x the coverage:

| | RSS | News sitemaps |
|---|---|---|
| Articles per run | ~120 | ~1200 |
| Sections | one per feed | all |
| Czech sources | 4 | 8 |
| Summaries | yes | via `enrich.py` |

Sources are configured in `fetcher.py:SOURCES`: Novinky, Seznam Zprávy,
Sport.cz, ČT24, iROZHLAS, iDNES, Blesk, Aktuálně.cz.

All of them are Czech outlets. World news comes from their foreign desks — the
`zahraniční`/`svět` sections, mapped in `WORLD_SECTIONS` — which is what a Czech
quiz audience actually saw that week, and the bizarre stories that make the best
questions get carried by Novinky and iDNES anyway.

Two quirks worth knowing, both handled in `fetcher.py`:

- iDNES declares `windows-1250` in its XML prolog but serves UTF-8, and uses the
  `n:` namespace prefix where everyone else uses `news:`.
- Blesk's `sitemap-news.xml` is a sitemap *index*, not a urlset.

## Selection

A day yields ~400 Czech and ~120 world articles, far more than the LLM budget,
so `selector.py` thins them down. It drops near-duplicate stories, ranks
headlines with a cheap heuristic mirroring the scoring prompt (specific names,
numbers, records, oddities; penalties for opinion and "talks continue"), then
spreads the budget over both days and **sources**.

The source half of that matters: Sport.cz files ~124 of the ~400 Czech articles
a day, and match reports are exactly what the heuristic rewards, so without it
the LLM was handed a sports round instead of a news round — 7 of the top 8
candidates were sport. Interleaving sources puts sport at ~14%, about its share
of `examples.json`.

## Usage

```bash
python main.py daily                 # score yesterday -> days/<date>.json
python main.py daily --date 2026-10-02
python main.py daily --dry-run       # fetch + select only, no LLM
python main.py daily --force         # redo a day that already has a file
python main.py week                  # merge + re-rank last week -> facts/
python main.py week --weeks-ago 0    # the current week so far
python main.py week --no-rerank      # order by daily score, no LLM call
python main.py week --top 15         # keep 15 instead of 20
python main.py prune                 # drop day files older than 8 weeks
python main.py prune --dry-run       # list what would go, delete nothing
python main.py prune --keep-weeks 4  # keep a shorter history
python main.py stats                 # what is in days/
python render.py                     # rebuild index.html from the newest facts file
```

Set `GROQ_API_KEY` in `.env`.

A date older than ~2 days cannot be backfilled — news sitemaps do not reach
further back, and nothing else is stored. If a daily run is missed, that day is
simply absent, and the weekly run names the missing days rather than quietly
reporting a thinner week.

## Staying on the Groq free tier

The model is `qwen/qwen3.8-27b`. `llama-3.3-70b-versatile` is no longer served
on Groq, and of what remains qwen is the right fit: it answers in Czech, stays
concise and returns clean JSON. The gpt-oss models spend roughly five times
more completion tokens per article, overrun `max_tokens` mid-array and lose the
whole batch to truncated JSON.

The published limits are 30 RPM, 1,000 RPD, 8k TPM, 200k TPD — plus one the
tables do not mention and which turns out to be the binding constraint:

**OTPM, output tokens per minute, capped at 1,000.** It is checked per request
against `max_tokens`, and an oversized request is refused outright:

```
Request too large ... on output tokens per minute (OTPM):
Limit 1000, Requested 1500. ... reduce max_tokens ... and try again
```

That is permanent, not throttling — retrying the identical request never
succeeds. So `MAX_TOKENS` stays at 1,000, and since a 20-article batch measured
1,738 completion tokens (nearly double the cap), batches are 10.

Measured against the live API per 10-article batch: ~2,140 prompt + ~870
completion = ~3,010 total.

| | Per run | Limit | |
|---|---|---|---|
| Daily: tokens | ~54,000 | 200,000 /day | 27% |
| Daily: requests | 18 | 1,000 /day | |
| Daily: output/min | ~870 | **1,000 (OTPM)** | the real ceiling |
| Weekly: tokens | ~4,000 | 200,000 /day | 2% |

~870 output tokens per call against 1,000 a minute allows roughly one call a
minute, hence `BATCH_SLEEP` of 60s — so 180 articles takes about 18 minutes.
Lower `SCORE_BUDGET` if that is too long; TPD is only 27% used, so the cap on
coverage is wall-clock time, not tokens.

Both call sites detect a response truncated at `max_tokens`
(`finish_reason == "length"`) and say so, because the symptom otherwise looks
like a model that cannot follow the format. `_wait_from_error` tells a
per-request refusal apart from an ordinary throttle and does not retry the
former.

The knobs are `SCORE_BUDGET`, `KEEP_PER_DAY`, `DAILY_MIN_SCORE`,
`WEEKLY_SHORTLIST` and `WEEKLY_TOP` in `analyzer.py`; the budget math is
documented in the comment block above them.

## The page

`render.py` writes `index.html` for the newest week plus `weeks/YYYY-Wnn.html`
for every week in `facts/`, so nothing is overwritten out of existence — each
Sunday adds a page instead of replacing the only one. Every page carries a week
picker across the top; weeks older than per-day storage have no day strip.

Within a page there is a tab per view:

```
[ Tyden 20 ] [ Ne 4.10. 20 ] [ Po 5.10. 20 ] [ St 7.10. - ] ...
```

- **Tyden** — the re-ranked top facts, the ones meant for the quiz.
- **one tab per day** — everything that day's run kept, before the week's
  head-to-head re-rank thinned it down. Useful for "what happened on Tuesday?"
  and for seeing what the re-rank passed over. A day with no file (never run,
  or pruned) shows a disabled tab rather than vanishing.

Ticking a fact still records feedback for `learn.py`, and the selection is
shared across views: the same story ticked in the week view lights up in its day
view too, and the download contains it once. The tick state and the last-opened
tab persist per week in `localStorage`.

## Pruning

Day files are tiny — a full year is ~3.3 MB, less than one day of the
`articles.json` this replaced — so pruning is optional. `python main.py prune`
exists if you want a bound on it anyway:

```
days/: 8 files | cutoff 2026-08-08 (keeping 8 weeks)
  3 too recent to touch
  2 old but their week was never rendered — kept:
      2026-07-06  (no facts/2026-W27.json)  use --force to delete anyway
  Deleting 3 file(s), 1,083 bytes:
      2026-05-04  (2026-W18 rendered)
```

A day file is deleted only when it is older than `--keep-weeks` **and** its week
has a `facts/YYYY-Wnn.json` to show for it. That second condition matters: the
day files are the only archive of the ~140 weekly candidates behind each
rendered top-20, and once they are gone a week cannot be rebuilt — sitemaps
reach back ~2 days and no articles are stored. `--force` overrides the check and
says so in the output. `facts/` files are never touched.

## Automation

- `.github/workflows/fetch_articles.yml` — daily at 07:00 UTC, scores yesterday
  and commits `days/<date>.json`. Takes a `date` input for a manual re-run.
- `.github/workflows/weekly_analysis.yml` — Sundays at 10:00 UTC, merges the
  week, writes `facts/YYYY-Wnn.json`, renders `index.html`. Saturday's day file
  lands at 07:00 UTC the same morning, so all seven exist by then.

Both need the `GROQ_API_KEY` secret.

Pruning is not automated. To wire it in, add a step to the weekly workflow after
the facts file is written, so the week being pruned is already rendered:

```yaml
      - name: Prune old day files
        run: python main.py prune --keep-weeks 8
```
