#!/usr/bin/env python3
"""
Debug first dedupe: run each step in isolation (build payload, call OpenAI, parse)
with just title + date (no body fetches). See where it breaks.

Usage:
  python3 scripts/debug_first_dedupe.py Lark
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _date_str(it):
    raw = it.get("date")
    if raw is None:
        return "no date"
    if hasattr(raw, "strftime"):
        return raw.strftime("%Y-%m-%d")
    s = (raw if isinstance(raw, str) else str(raw)).strip()
    return s[:10] if len(s) >= 10 else (s or "no date")


def main():
    from app.config import settings
    from app.collectors.global_press import collect_google_news_items, collect_prnewswire_items
    from app.llm_structured import _classify_press_headlines_with_llm

    if not settings.openai_api_key:
        print("OPENAI_API_KEY required.")
        sys.exit(1)

    competitor_name = "Lark Hotels" if (len(sys.argv) > 1 and sys.argv[1].lower() == "lark") else (sys.argv[1] if len(sys.argv) > 1 else "Lark Hotels")
    press_search = "Lark Hotels" if "lark" in competitor_name.lower() else competitor_name

    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    raw = []
    try:
        gn = collect_google_news_items(press_search, max_items=50, window_days=90)
        raw.extend(gn)
    except Exception as e:
        print(f"Google News failed: {e}")
    try:
        prn = collect_prnewswire_items(press_search, max_items=40, window_days=90)
        raw.extend(prn)
    except Exception:
        pass

    def _parse_dt(v):
        if v is None: return None
        if hasattr(v, "year"): return v
        try:
            s = str(v)[:19].replace("Z", "+00:00")
            return datetime.fromisoformat(s) if s else None
        except Exception:
            return None

    filtered = []
    for it in raw:
        dt = _parse_dt(it.get("date"))
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt and dt >= cutoff:
            filtered.append(it)
    filtered = filtered[:50]
    classified = _classify_press_headlines_with_llm(competitor_name, filtered)
    items = [c for c in classified if (c.get("topic") or "").strip().lower() != "irrelevant"]

    print("=" * 70)
    print("STEP 1 — Build payload (title, date only — no body)")
    print("=" * 70)
    lines = []
    for i, it in enumerate(items):
        title = (it.get("title") or "").strip() or "—"
        date_s = _date_str(it)
        lines.append(f"{i}: {title} | {date_s}")
    payload = "\n".join(lines)
    print(payload[:2000])
    if len(payload) > 2000:
        print(f"\n... (total {len(payload)} chars)")
    print(f"\nTotal: {len(items)} items")

    print("\n" + "=" * 70)
    print("STEP 2 — Call OpenAI")
    print("=" * 70)
    system = (
        "You filter and deduplicate a press list for one hospitality/hotels company. "
        "Return ONLY a JSON array of 0-based indices to KEEP. Example: [0, 2, 5]. No other text.\n\n"
        "EXCLUDE (omit their indices): Articles that have NOTHING to do with hospitality, hotels, real estate, "
        "fundraising, or company/business. Wrong entity (e.g. person/place with same name).\n"
        "For Lark: only Lark Hotels / Lark Hospitality; exclude persons/places named Lark.\n\n"
        "MERGE (keep one index per story): Same story from different outlets = one index; prefer best source. "
        "E.g. 'Four Lark Hotels to debut' and 'Lark Expands with Four New Hotels' = same story, keep one.\n\n"
        "Your array MUST be shorter when there are duplicates. Order: most recent first. Return only the JSON array."
    )
    company_note = f"Target: {competitor_name}.\n\n"
    user = company_note + "Items (index: title | date). Merge same story, exclude irrelevant.\n\n" + payload + "\n\nReturn ONLY a JSON array of indices to KEEP, e.g. [0,2,5]."

    from app.llm_structured import _openai_client
    client = _openai_client()
    if not client:
        print("No OpenAI client (check OPENAI_API_KEY and openai package). Run: pip install openai")
        return
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2000,
        temperature=0.1,
    )
    content = (resp.choices[0].message.content or "").strip()
    print("Raw LLM response:")
    print(repr(content))
    print(f"\nResponse length: {len(content)} chars")

    print("\n" + "=" * 70)
    print("STEP 3 — Parse result")
    print("=" * 70)

    def parse_indices(text, n_items):
        if not text or n_items <= 0:
            return None
        t = text.strip()
        if "```" in t:
            t = re.sub(r"^```\w*\n?", "", t).rstrip("`\n")
        try:
            data = json.loads(t)
            if isinstance(data, list):
                idx = [int(x) for x in data if isinstance(x, (int, float)) and 0 <= int(x) < n_items]
                return list(dict.fromkeys(idx))
        except Exception as e:
            print(f"json.loads failed: {e}")
        for m in re.finditer(r"\[[\d\s,]+\]", t):
            try:
                arr = json.loads(m.group(0))
                idx = [int(x) for x in arr if isinstance(x, (int, float)) and 0 <= int(x) < n_items]
                if idx:
                    return list(dict.fromkeys(idx))
            except Exception:
                continue
        return None

    indices = parse_indices(content, len(items))
    if indices is None:
        print("Parse FAILED — could not extract indices.")
    else:
        print(f"Parsed indices ({len(indices)}): {indices[:30]}{'...' if len(indices) > 30 else ''}")
        result = [items[i] for i in indices]
        print(f"\nKEPT ({len(result)}):")
        for i, it in enumerate(result[:15]):
            print(f"  {i+1}. {(it.get('title') or '')[:65]}")
        if len(result) > 15:
            print(f"  ... and {len(result) - 15} more")
        print(f"\nDropped: {len(items) - len(result)} items")

    print("\nDone.")


if __name__ == "__main__":
    main()
