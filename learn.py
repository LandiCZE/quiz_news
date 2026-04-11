"""
Learns from feedback: adds good facts to examples.json so the
scoring prompt gets better over time.

Usage:
    python learn.py feedback_2026-W15.json
"""

import json
import sys
from pathlib import Path

EXAMPLES_FILE = Path(__file__).parent / "examples.json"

# Map category to a why_good template based on what we know works
CATEGORY_WHY = {
    "cz":    "confirmed good by user feedback",
    "world": "confirmed good by user feedback",
}


def load_examples() -> list[dict]:
    if EXAMPLES_FILE.exists():
        return json.loads(EXAMPLES_FILE.read_text(encoding="utf-8"))
    return []


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python learn.py feedback_YYYY-WNN.json")
        sys.exit(1)

    feedback_path = Path(sys.argv[1])
    if not feedback_path.exists():
        print(f"File not found: {feedback_path}")
        sys.exit(1)

    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    if not feedback:
        print("No items in feedback file.")
        return

    examples = load_examples()
    existing_facts = {e["question"] for e in examples}

    added = 0
    for item in feedback:
        fact = item["fact"]
        # Skip if already in examples (exact match)
        if fact in existing_facts:
            continue

        examples.append({
            "question": fact,
            "answer":   "",    # user can fill in later
            "category": item.get("category", "unknown"),
            "why_good": f"user-confirmed (week {item.get('week', '?')}, score {item.get('score', '?')})",
            "source":   item.get("source", ""),
            "url":      item.get("url", ""),
        })
        existing_facts.add(fact)
        added += 1
        print(f"  + {fact}")

    EXAMPLES_FILE.write_text(
        json.dumps(examples, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nAdded {added} new examples to {EXAMPLES_FILE}  ({len(examples)} total)")
    if added:
        print("Tip: commit examples.json and the next weekly analysis will use the updated examples.")


if __name__ == "__main__":
    main()
