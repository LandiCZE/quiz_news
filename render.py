"""
Generates the published site from the files in facts/ and days/.

    index.html              the newest week
    weeks/YYYY-Wnn.html     one page per week

Every page carries a week picker, so older weeks stay reachable instead of
being overwritten each Sunday. A week page shows the re-ranked top facts plus
one tab per day from that week's day files; weeks older than per-day storage
have no day strip at all.

Run after the weekly analysis: python render.py

The template uses string.Template ($name) rather than an f-string so CSS and JS
braces can be written literally instead of doubled.
"""

import html
import json
import sys
from datetime import date as Date, datetime, timedelta
from pathlib import Path
from string import Template

import days

ROOT       = Path(__file__).parent
FACTS_DIR  = ROOT / "facts"
WEEKS_DIR  = ROOT / "weeks"
INDEX_FILE = ROOT / "index.html"

CATEGORY_LABEL = {"cz": "🇨🇿 Czech", "world": "🌍 World"}
WEEKDAY_CS = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"]

SCORE_COLOR = {
    10: "#1a7f37", 9: "#1a7f37",
    8:  "#2d8a1e",
    7:  "#5a8a00",
    6:  "#8a6d00",
}


def load_weeks() -> list[dict]:
    """Every week in facts/, newest first. Unreadable files are skipped."""
    weeks = []
    for path in sorted(FACTS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            start = datetime.fromisoformat(data["week_start"]).date()
            end = datetime.fromisoformat(data["week_end"]).date()
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            print(f"  skipping {path.name}: {exc}")
            continue
        weeks.append({
            "key": path.stem,
            "path": path,
            "data": data,
            "start": start,
            "last": end - timedelta(days=1),
        })
    return weeks


def score_badge(score: int) -> str:
    color = SCORE_COLOR.get(score, "#666")
    return f'<span class="badge" style="background:{color}">{score}/10</span>'


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _rows(numbered: list[tuple[int, dict]], view: str) -> str:
    """Table rows for one view.

    The index comes in already assigned from the view's full fact list. It
    cannot be re-derived here: rows are rendered one category at a time, so a
    local enumerate() would restart at 0 for the world section and collide with
    the Czech ids — ticking one row would mark the other and the download would
    map the id to the wrong fact.
    """
    rows = ""
    for index, fact in numbered:
        fid = f"{view}:{index}"
        url = fact.get("url", "")
        link = (f'<a href="{esc(url)}" target="_blank" rel="noopener">↗</a>'
                if url else "")
        score = fact.get("final_score", fact.get("score", 0))
        note = fact.get("why") or fact.get("reason", "")
        rows += f"""
            <tr data-id="{esc(fid)}">
              <td class="cb-cell">
                <input type="checkbox" class="fb-check" data-id="{esc(fid)}">
              </td>
              <td>{score_badge(score)}</td>
              <td class="fact">{esc(fact.get('fact', ''))} {link}</td>
              <td class="meta">{esc(fact.get('source', ''))}<br>
                  <span class="reason">{esc(note)}</span></td>
            </tr>"""
    return rows


def _panel(facts: list[dict], view: str, heading: str, subtitle: str,
           empty_text: str, active: bool) -> str:
    by_category: dict[str, list[tuple[int, dict]]] = {}
    for index, fact in enumerate(facts):
        by_category.setdefault(fact.get("category", "cz"), []).append((index, fact))

    sections = ""
    for category in ("cz", "world"):
        items = by_category.get(category, [])
        if not items:
            continue
        sections += f"""
        <section>
          <h2>{CATEGORY_LABEL.get(category, category)} <span class="count">{len(items)}</span></h2>
          <table><tbody>{_rows(items, view)}</tbody></table>
        </section>"""

    if not sections:
        sections = f'<p class="empty">{esc(empty_text)}</p>'

    return f"""
      <div class="panel{' active' if active else ''}" data-panel="{esc(view)}">
        <div class="panel-head">
          <h1>{esc(heading)}</h1>
          <p>{esc(subtitle)}</p>
        </div>
        {sections}
      </div>"""


def _picker(weeks: list[dict], active_key: str, prefix: str) -> str:
    """Chips linking every week, newest first. `prefix` is "" for pages that
    sit inside weeks/ and "weeks/" for index.html at the repo root."""
    chips = ""
    for week in weeks:
        number = week["key"].split("-W")[-1]
        label = f'W{number} &middot; {week["start"].day}.{week["start"].month}.'
        title = f'{week["start"]} – {week["last"]}'
        if week["key"] == active_key:
            chips += f'<span class="wk active" title="{esc(title)}">{label}</span>'
        else:
            chips += (f'<a class="wk" href="{esc(prefix + week["key"])}.html" '
                      f'title="{esc(title)}">{label}</a>')
    return chips


def render_page(week: dict, weeks: list[dict], prefix: str) -> str:
    data = week["data"]
    week_facts = data["facts"]
    week_key = week["key"]
    week_start = week["start"]
    last_day = week["last"]

    # --- gather the week's day files ---
    day_views: list[dict] = []
    cursor = week_start
    while cursor <= last_day:
        stored = days.load_day(cursor)
        day_views.append({
            "date": cursor,
            "view": cursor.isoformat(),
            "facts": stored.get("facts", []) if stored else [],
            "stats": stored.get("stats", {}) if stored else {},
            "present": stored is not None,
        })
        cursor += timedelta(days=1)

    # --- tabs ---
    has_days = any(d["present"] for d in day_views)

    tabs = (f'<button class="tab active" data-tab="week">Týden'
            f'<span class="tab-n">{len(week_facts)}</span></button>')
    for day in (day_views if has_days else []):
        classes = "tab" if day["present"] else "tab missing"
        label = f'{WEEKDAY_CS[day["date"].weekday()]} {day["date"].day}.{day["date"].month}.'
        count = (f'<span class="tab-n">{len(day["facts"])}</span>'
                 if day["present"] else '<span class="tab-n">–</span>')
        disabled = "" if day["present"] else " disabled title=\"no day file\""
        tabs += (f'<button class="{classes}" data-tab="{day["view"]}"{disabled}>'
                 f'{label}{count}</button>')

    # --- panels ---
    candidates = data.get("candidates")
    subtitle = (f"Re-ranked top {len(week_facts)} from {candidates} candidates"
                if candidates else f"{len(week_facts)} facts")

    panels = _panel(
        week_facts, "week",
        "Týden – nejlepší zprávy",
        f"{subtitle} · {week_start} – {last_day}",
        "No facts for this week.",
        active=True,
    )
    for day in (day_views if has_days else []):
        if day["present"]:
            s = day["stats"]
            subtitle = (f"Kept {s.get('kept', len(day['facts']))} of "
                        f"{s.get('scored', '?')} scored · "
                        f"{s.get('that_day', '?')} articles published that day")
        else:
            subtitle = "No day file for this date."
        panels += _panel(
            day["facts"], day["view"],
            f'{WEEKDAY_CS[day["date"].weekday()]} {day["date"].isoformat()}',
            subtitle,
            "No facts kept for this day.",
            active=False,
        )

    # --- data for the download button, keyed by the same ids as the rows ---
    catalogue: dict[str, dict] = {}
    for index, fact in enumerate(week_facts):
        catalogue[f"week:{index}"] = {**fact, "view": "week"}
    for day in day_views:
        for index, fact in enumerate(day["facts"]):
            catalogue[f'{day["view"]}:{index}'] = {**fact, "view": day["view"]}

    return Template(PAGE).safe_substitute(
        title=f"Quiz News — {week_start} – {last_day}",
        picker=_picker(weeks, week_key, prefix),
        week_key=week_key,
        week_range=f"{week_start} – {last_day}",
        tabs=tabs,
        panels=panels,
        catalogue=json.dumps(catalogue, ensure_ascii=False),
        week_key_json=json.dumps(week_key),
        source_file=week["path"].name,
    )


PAGE = """<!DOCTYPE html>
<html lang="cs">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>$title</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, sans-serif;
      background: #f6f8fa;
      color: #1a1a1a;
      padding: 2rem 1rem;
      max-width: 880px;
      margin: 0 auto;
    }
    .masthead { margin-bottom: 1rem; }
    .masthead h1 { font-size: 1.5rem; font-weight: 700; }
    .masthead p { color: #555; margin-top: .25rem; font-size: .9rem; }

    .archive {
      display: flex; flex-wrap: wrap; gap: .3rem; align-items: center;
      margin: 1rem 0 1.25rem; padding-bottom: 1rem;
      border-bottom: 1px solid #e1e4e8;
    }
    .archive-label {
      font-size: .74rem; color: #666; margin-right: .3rem;
      text-transform: uppercase; letter-spacing: .04em;
    }
    .wk {
      font-size: .76rem; padding: .22rem .45rem; border-radius: 4px;
      text-decoration: none; color: #0969da; background: #fff;
      border: 1px solid #d8dee4; white-space: nowrap;
    }
    .wk:hover { border-color: #0969da; background: #f0f6ff; }
    .wk.active {
      background: #1a1a1a; border-color: #1a1a1a; color: #fff; font-weight: 600;
    }

    .tabs {
      display: flex; flex-wrap: wrap; gap: .35rem;
      margin: 0 0 1.5rem;
    }
    .tab {
      display: inline-flex; align-items: center; gap: .4rem;
      background: #fff; border: 1px solid #d8dee4; color: #333;
      padding: .45rem .7rem; border-radius: 6px; cursor: pointer;
      font-size: .85rem; font-family: inherit;
    }
    .tab:hover:not(:disabled) { border-color: #0969da; color: #0969da; }
    .tab.active {
      background: #0969da; border-color: #0969da; color: #fff; font-weight: 600;
    }
    .tab:disabled { opacity: .45; cursor: default; }
    .tab-n {
      background: rgba(0,0,0,.08); border-radius: 10px;
      padding: 0 .4rem; font-size: .75rem; font-weight: 600;
    }
    .tab.active .tab-n { background: rgba(255,255,255,.25); }

    .toolbar {
      display: flex; align-items: center; gap: .75rem;
      background: #fff; border-radius: 8px; padding: .75rem 1rem;
      box-shadow: 0 1px 3px rgba(0,0,0,.08);
      margin-bottom: 1.75rem; font-size: .9rem; color: #555;
    }
    .toolbar strong { color: #1a1a1a; }
    #btn-download {
      margin-left: auto;
      background: #0969da; color: #fff; border: none;
      padding: .45rem 1rem; border-radius: 6px; cursor: pointer;
      font-size: .88rem; font-weight: 600; font-family: inherit;
    }
    #btn-download:hover { background: #0550ae; }
    #btn-download:disabled { background: #8ab; cursor: default; }

    .panel { display: none; }
    .panel.active { display: block; }
    .panel-head { margin-bottom: 1.25rem; }
    .panel-head h1 { font-size: 1.2rem; font-weight: 700; }
    .panel-head p { color: #666; font-size: .85rem; margin-top: .2rem; }

    section { margin-bottom: 2.25rem; }
    h2 { font-size: 1.05rem; font-weight: 600; margin-bottom: .7rem; }
    .count { color: #888; font-weight: 400; font-size: .85rem; }
    table { width: 100%; border-collapse: collapse; background: #fff;
            border-radius: 8px; overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,.08); }
    tr { border-bottom: 1px solid #eee; transition: background .15s; }
    tr:last-child { border-bottom: none; }
    tr.checked { background: #f0fff4; }
    td { padding: .7rem .9rem; vertical-align: top; }
    .cb-cell { width: 36px; text-align: center; }
    .cb-cell input { width: 17px; height: 17px; cursor: pointer; accent-color: #1a7f37; }
    td:nth-child(2) { width: 60px; text-align: center; }
    .badge { display: inline-block; color: #fff; font-weight: 700;
             font-size: .8rem; padding: .2rem .5rem;
             border-radius: 4px; white-space: nowrap; }
    .fact { font-size: .97rem; line-height: 1.45; }
    .fact a { color: #0969da; text-decoration: none; margin-left: .3rem; }
    .fact a:hover { text-decoration: underline; }
    .meta { font-size: .8rem; color: #555; width: 160px; }
    .reason { color: #888; font-style: italic; }
    .empty { color: #888; font-size: .9rem; background: #fff; padding: 1rem;
             border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
    footer { margin-top: 3rem; font-size: .8rem; color: #999; text-align: center; }
    @media (max-width: 600px) { .meta { display: none; } }
  </style>
</head>
<body>
  <div class="masthead">
    <h1>Quiz News</h1>
    <p>$week_range</p>
  </div>

  <nav class="archive"><span class="archive-label">Týdny</span>$picker</nav>

  <nav class="tabs">$tabs</nav>

  <div class="toolbar">
    <span>Tick the facts that were <strong>actually good</strong> for the quiz</span>
    <span id="checked-count">0 selected</span>
    <button id="btn-download" disabled>&#11015; Download feedback</button>
  </div>

  $panels

  <footer>Generated from $source_file &middot;
    <a href="https://github.com/LandiCZE/quiz_news">source</a></footer>

  <script>
    const WEEK_KEY  = $week_key_json;
    const CATALOGUE = $catalogue;
    const TAB_KEY   = WEEK_KEY + ':tab';

    // --- tabs ---
    const tabs = document.querySelectorAll('.tab');
    const panels = document.querySelectorAll('.panel');

    function showTab(name) {
      let found = false;
      panels.forEach(p => {
        const on = p.dataset.panel === name;
        p.classList.toggle('active', on);
        if (on) found = true;
      });
      if (!found) return false;
      tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === name));
      try { localStorage.setItem(TAB_KEY, name); } catch (e) {}
      return true;
    }

    tabs.forEach(t => t.addEventListener('click', () => showTab(t.dataset.tab)));

    // Reopen whichever tab was last viewed. Storage can be empty or throw in a
    // private window, so the week view stays the fallback.
    try {
      const last = localStorage.getItem(TAB_KEY);
      if (last) showTab(last);
    } catch (e) {}

    // --- feedback selection, shared across every view ---
    let checked = new Set();
    try {
      checked = new Set(JSON.parse(localStorage.getItem(WEEK_KEY) || '[]'));
    } catch (e) {}

    function save() {
      try { localStorage.setItem(WEEK_KEY, JSON.stringify([...checked])); } catch (e) {}
    }

    // A story selected in both the week view and its day view occupies two ids,
    // so count distinct facts — otherwise one tick reads as "2 selected".
    function factKey(fact) {
      return fact.url || fact.fact;
    }

    function distinctChecked() {
      const keys = new Set();
      for (const id of checked) {
        const fact = CATALOGUE[id];
        if (fact) keys.add(factKey(fact));
      }
      return keys;
    }

    function updateUI() {
      const n = distinctChecked().size;
      document.getElementById('checked-count').textContent =
        n === 0 ? '0 selected' : n + ' selected';
      document.getElementById('btn-download').disabled = n === 0;
    }

    // The same story can appear in the week view and in its day view. Ticking
    // either marks both, so the download never contains it twice.
    function sameFactIds(id) {
      const fact = CATALOGUE[id];
      if (!fact) return [id];
      const key = factKey(fact);
      return Object.keys(CATALOGUE).filter(
        other => factKey(CATALOGUE[other]) === key);
    }

    function paint() {
      document.querySelectorAll('.fb-check').forEach(cb => {
        const on = checked.has(cb.dataset.id);
        cb.checked = on;
        cb.closest('tr').classList.toggle('checked', on);
      });
    }

    document.querySelectorAll('.fb-check').forEach(cb => {
      cb.addEventListener('change', () => {
        const ids = sameFactIds(cb.dataset.id);
        if (cb.checked) ids.forEach(i => checked.add(i));
        else ids.forEach(i => checked.delete(i));
        save();
        paint();
        updateUI();
      });
    });

    paint();
    updateUI();

    // --- download feedback JSON (consumed by learn.py) ---
    document.getElementById('btn-download').addEventListener('click', () => {
      const seen = new Set();
      const selected = [];
      for (const id of checked) {
        const f = CATALOGUE[id];
        if (!f) continue;
        const key = factKey(f);
        if (seen.has(key)) continue;
        seen.add(key);
        selected.push({
          week:     WEEK_KEY,
          day:      f.date || '',
          fact:     f.fact,
          source:   f.source,
          category: f.category,
          score:    f.final_score || f.score,
          url:      f.url || '',
        });
      }
      const blob = new Blob([JSON.stringify(selected, null, 2)],
                            {type: 'application/json'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'feedback_' + WEEK_KEY + '.json';
      a.click();
    });
  </script>
</body>
</html>"""


def main() -> None:
    weeks = load_weeks()
    if not weeks:
        print("No facts files found in facts/. Run the analysis first.")
        sys.exit(1)

    WEEKS_DIR.mkdir(exist_ok=True)
    for week in weeks:
        # Pages inside weeks/ link to their siblings, so no path prefix.
        path = WEEKS_DIR / f'{week["key"]}.html'
        path.write_text(render_page(week, weeks, prefix=""), encoding="utf-8")

    newest = weeks[0]
    INDEX_FILE.write_text(
        render_page(newest, weeks, prefix="weeks/"), encoding="utf-8")

    total = sum(len(w["data"]["facts"]) for w in weeks)
    print(f"Written index.html (from {newest['path'].name}) and "
          f"{len(weeks)} week pages in weeks/ — {total} facts archived")


if __name__ == "__main__":
    main()
