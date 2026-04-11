"""
Generates index.html from the latest facts JSON file in facts/.
Run after weekly analysis: python render.py
"""

import json
import sys
from pathlib import Path

FACTS_DIR = Path(__file__).parent / "facts"
OUT_FILE  = Path(__file__).parent / "index.html"

CATEGORY_LABEL = {"cz": "🇨🇿 Czech", "world": "🌍 World"}

SCORE_COLOR = {
    10: "#1a7f37", 9: "#1a7f37",
    8:  "#2d8a1e",
    7:  "#5a8a00",
    6:  "#8a6d00",
}


def latest_facts_file() -> Path | None:
    files = sorted(FACTS_DIR.glob("*.json"), reverse=True)
    return files[0] if files else None


def score_badge(score: int) -> str:
    color = SCORE_COLOR.get(score, "#666")
    return f'<span class="badge" style="background:{color}">{score}/10</span>'


def render(facts_path: Path) -> str:
    data      = json.loads(facts_path.read_text(encoding="utf-8"))
    facts     = data["facts"]
    week_s    = data["week_start"][:10]
    week_e    = data["week_end"][:10]
    week_key  = facts_path.stem   # e.g. "2026-W15" — used as localStorage key

    by_cat: dict[str, list] = {"cz": [], "world": []}
    for i, f in enumerate(facts):
        f["_id"] = i   # stable index for checkbox identification
        by_cat.setdefault(f["category"], []).append(f)

    sections = ""
    for cat in ("cz", "world"):
        items = by_cat.get(cat, [])
        if not items:
            continue
        rows = ""
        for f in items:
            fid  = f["_id"]
            link = f'<a href="{f["url"]}" target="_blank" rel="noopener">↗</a>' if f.get("url") else ""
            rows += f"""
            <tr id="row-{fid}" data-id="{fid}" data-fact="{f['fact'].replace('"', '&quot;')}"
                data-source="{f['source']}" data-category="{f['category']}"
                data-score="{f['score']}" data-url="{f.get('url','')}">
              <td class="cb-cell">
                <input type="checkbox" class="fb-check" id="cb-{fid}" data-id="{fid}">
              </td>
              <td>{score_badge(f["score"])}</td>
              <td class="fact">{f["fact"]} {link}</td>
              <td class="meta">{f["source"]}<br><span class="reason">{f["reason"]}</span></td>
            </tr>"""
        sections += f"""
        <section>
          <h2>{CATEGORY_LABEL.get(cat, cat)}</h2>
          <table><tbody>{rows}</tbody></table>
        </section>"""

    # Embed the full facts list as JSON for the download function
    facts_json = json.dumps(facts, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quiz News — {week_s} – {week_e}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: system-ui, sans-serif;
      background: #f6f8fa;
      color: #1a1a1a;
      padding: 2rem 1rem;
      max-width: 860px;
      margin: 0 auto;
    }}
    header {{ margin-bottom: 1.5rem; }}
    header h1 {{ font-size: 1.6rem; font-weight: 700; }}
    header p  {{ color: #555; margin-top: .3rem; font-size: .95rem; }}
    .toolbar {{
      display: flex; align-items: center; gap: .75rem;
      background: #fff; border-radius: 8px; padding: .75rem 1rem;
      box-shadow: 0 1px 3px rgba(0,0,0,.08);
      margin-bottom: 2rem; font-size: .9rem; color: #555;
    }}
    .toolbar strong {{ color: #1a1a1a; }}
    #btn-download {{
      margin-left: auto;
      background: #0969da; color: #fff; border: none;
      padding: .45rem 1rem; border-radius: 6px; cursor: pointer;
      font-size: .88rem; font-weight: 600;
    }}
    #btn-download:hover {{ background: #0550ae; }}
    #btn-download:disabled {{ background: #8ab; cursor: default; }}
    section   {{ margin-bottom: 2.5rem; }}
    h2        {{ font-size: 1.15rem; font-weight: 600; margin-bottom: .75rem; }}
    table     {{ width: 100%; border-collapse: collapse; background: #fff;
                 border-radius: 8px; overflow: hidden;
                 box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
    tr        {{ border-bottom: 1px solid #eee; transition: background .15s; }}
    tr:last-child {{ border-bottom: none; }}
    tr.checked {{ background: #f0fff4; }}
    td        {{ padding: .7rem .9rem; vertical-align: top; }}
    .cb-cell  {{ width: 36px; text-align: center; }}
    .cb-cell input {{ width: 17px; height: 17px; cursor: pointer; accent-color: #1a7f37; }}
    td:nth-child(2) {{ width: 60px; text-align: center; }}
    .badge    {{ display: inline-block; color: #fff; font-weight: 700;
                 font-size: .8rem; padding: .2rem .5rem;
                 border-radius: 4px; white-space: nowrap; }}
    .fact     {{ font-size: .97rem; line-height: 1.45; }}
    .fact a   {{ color: #0969da; text-decoration: none; margin-left: .3rem; }}
    .fact a:hover {{ text-decoration: underline; }}
    .meta     {{ font-size: .8rem; color: #555; width: 160px; }}
    .reason   {{ color: #888; font-style: italic; }}
    footer    {{ margin-top: 3rem; font-size: .8rem; color: #999; text-align: center; }}
    @media (max-width: 600px) {{ .meta {{ display: none; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Quiz News</h1>
    <p>Quiz-worthy facts for the week of {week_s} – {week_e}</p>
  </header>

  <div class="toolbar">
    <span>Tick the facts that were <strong>actually good</strong> for the quiz</span>
    <span id="checked-count">0 selected</span>
    <button id="btn-download" disabled>⬇ Download feedback</button>
  </div>

  {sections}

  <footer>Generated from {facts_path.name} &middot; <a href="https://github.com/LandiCZE/quiz_news">source</a></footer>

  <script>
    const WEEK_KEY  = {json.dumps(week_key)};
    const ALL_FACTS = {facts_json};

    // --- persist checkboxes in localStorage ---
    const saved = JSON.parse(localStorage.getItem(WEEK_KEY) || '[]');
    const checked = new Set(saved);

    function updateUI() {{
      const count = checked.size;
      document.getElementById('checked-count').textContent =
        count === 0 ? '0 selected' : count + ' selected';
      document.getElementById('btn-download').disabled = count === 0;
    }}

    document.querySelectorAll('.fb-check').forEach(cb => {{
      const id = parseInt(cb.dataset.id);
      cb.checked = checked.has(id);
      cb.closest('tr').classList.toggle('checked', cb.checked);

      cb.addEventListener('change', () => {{
        if (cb.checked) checked.add(id); else checked.delete(id);
        cb.closest('tr').classList.toggle('checked', cb.checked);
        localStorage.setItem(WEEK_KEY, JSON.stringify([...checked]));
        updateUI();
      }});
    }});

    updateUI();

    // --- download feedback JSON ---
    document.getElementById('btn-download').addEventListener('click', () => {{
      const selected = ALL_FACTS.filter(f => checked.has(f._id)).map(f => ({{
        week:     WEEK_KEY,
        fact:     f.fact,
        source:   f.source,
        category: f.category,
        score:    f.score,
        url:      f.url || '',
      }}));
      const blob = new Blob([JSON.stringify(selected, null, 2)],
                            {{type: 'application/json'}});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'feedback_' + WEEK_KEY + '.json';
      a.click();
    }});
  </script>
</body>
</html>"""


def main() -> None:
    path = latest_facts_file()
    if not path:
        print("No facts files found in facts/. Run the analysis first.")
        sys.exit(1)
    html = render(path)
    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"Written {OUT_FILE}  (from {path.name})")


if __name__ == "__main__":
    main()
