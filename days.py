"""Per-day fact storage — one small JSON file per calendar day.

Replaces the single growing articles.json. Each daily run scores that day's
articles and keeps only the facts worth keeping, so a day costs ~6 kB instead
of ~1.2 MB of raw articles.

Raw articles are deliberately *not* retained. Each run processes one closed
calendar day (selected by publication timestamp), so runs never overlap and no
URL bookkeeping is needed to avoid scoring the same article twice.

    days/2026-10-03.json
    {
      "date":  "2026-10-03",
      "generated": "2026-10-04T07:02:11+00:00",
      "stats": {"fetched": 571, "deduped": 523, "scored": 180, "kept": 20},
      "facts": [{"score": 9, "fact": ..., "reason": ..., "title": ...,
                 "source": ..., "category": ..., "url": ...}, ...]
    }
"""

import json
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path

DAYS_DIR = Path(__file__).parent / "days"


def day_path(day: Date) -> Path:
    return DAYS_DIR / f"{day.isoformat()}.json"


def save_day(day: Date, facts: list[dict], stats: dict) -> Path:
    DAYS_DIR.mkdir(exist_ok=True)
    payload = {
        "date": day.isoformat(),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": stats,
        "facts": facts,
    }
    path = day_path(day)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_day(day: Date) -> dict | None:
    path = day_path(day)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_range(start: datetime, end: datetime) -> tuple[list[dict], list[Date]]:
    """Facts from every day file in [start, end), plus the days that are missing.

    Missing days are returned rather than ignored so the weekly run can say
    out loud that a daily job did not run, instead of quietly reporting a
    thinner week.
    """
    facts: list[dict] = []
    missing: list[Date] = []

    for day in _days_between(start, end):
        data = load_day(day)
        if data is None:
            missing.append(day)
            continue
        for fact in data.get("facts", []):
            facts.append({**fact, "date": data["date"]})

    return facts, missing


def stats() -> dict:
    files = sorted(DAYS_DIR.glob("*.json")) if DAYS_DIR.exists() else []
    total = 0
    for path in files:
        total += len(json.loads(path.read_text(encoding="utf-8")).get("facts", []))
    return {
        "days": len(files),
        "facts": total,
        "oldest": files[0].stem if files else None,
        "newest": files[-1].stem if files else None,
        "bytes": sum(p.stat().st_size for p in files),
    }


def _days_between(start: datetime, end: datetime) -> list[Date]:
    days = []
    current = start.date()
    while current < end.date():
        days.append(current)
        current += timedelta(days=1)
    return days


def list_days() -> list[tuple[Date, Path]]:
    """Every day file on disk, oldest first, as (date, path)."""
    if not DAYS_DIR.exists():
        return []
    found = []
    for path in sorted(DAYS_DIR.glob("*.json")):
        try:
            found.append((Date.fromisoformat(path.stem), path))
        except ValueError:
            continue   # not a day file (e.g. a stray export)
    return found


def week_key(day: Date) -> str:
    """The facts/ file name covering this day, e.g. "2026-W40".

    Quiz weeks start on Sunday while ISO weeks start on Monday, so the key is
    taken from the Sunday that opens the week, matching how the weekly run
    names its output.
    """
    sunday = day - timedelta(days=(day.weekday() + 1) % 7)
    iso = sunday.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"
