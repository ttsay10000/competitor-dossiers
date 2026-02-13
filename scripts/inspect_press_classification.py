#!/usr/bin/env python3
"""
Run locally to see the full list of press articles and what is marked
irrelevant (or promo / not_about_company) vs included.

Usage (from project root):
  python3 scripts/inspect_press_classification.py
  python3 scripts/inspect_press_classification.py Lark
  python3 scripts/inspect_press_classification.py AvantStay

Uses the same sources as --local press: Google News + PR Newswire (90d window).
Requires OPENAI_API_KEY in .env for LLM classification.
"""
import json
import sys
from pathlib import Path

# Allow importing app when run as scripts/inspect_press_classification.py
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.collectors.global_press import collect_google_news_items, collect_prnewswire_items
from app.llm_structured import get_press_classification_for_inspection
from app.runner import get_press_competitors_list, LOCAL_PRESS_COMPETITORS


def _load_competitors(competitor_name=None):
    """
    Load (display_name, press_search_name): DB first (UI-added competitors), then seed_data, then fallback.
    """
    from_db = get_press_competitors_list(competitor_name)
    if from_db:
        return from_db
    try:
        with open(ROOT / "seed_data.json") as f:
            data = json.load(f)
        competitors = data.get("competitors", [])
        if not competitors:
            raise ValueError("no competitors")
        out = []
        for c in competitors:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            press_search = name
            for src in c.get("sources") or []:
                if (src.get("channel") or "").strip().lower() == "press":
                    opts = src.get("extra_options") or {}
                    if isinstance(opts, dict) and opts.get("press_search_name"):
                        press_search = (opts.get("press_search_name") or "").strip() or name
                    break
            out.append((name, press_search))
        if competitor_name:
            name_lower = (competitor_name or "").strip().lower()
            out = [(d, s) for d, s in out if name_lower in d.lower() or name_lower in s.lower()]
        if out:
            return out
    except Exception:
        pass
    out = list(LOCAL_PRESS_COMPETITORS)
    if competitor_name:
        name_lower = (competitor_name or "").strip().lower()
        out = [(d, s) for d, s in out if name_lower in d.lower() or name_lower in s.lower()]
    return out


def _parse_press_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except Exception:
            return None
    elif isinstance(value, str):
        val = value.strip()
        if not val:
            return None
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def main():
    competitor_name = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    competitors = _load_competitors(competitor_name)
    if not competitors:
        all_competitors = _load_competitors(None)
        opts = ", ".join(disp for disp, _ in (all_competitors or [])[:10])
        if (all_competitors or []) and len(all_competitors) > 10:
            opts += ", ..."
        print("No competitor matching {!r}. Options: {}.".format(competitor_name or "''", opts or "none"))
        sys.exit(1)

    window_days = 90
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    max_per_source = getattr(settings, "press_max_items_per_source", 30)
    max_raw = getattr(settings, "press_max_raw_items_per_competitor", 120)

    for row in competitors:
        display_name = row[0]
        press_search_name = row[1]
        google_news_search_phrases = row[2] if len(row) > 2 else None
        print("\n" + "=" * 80)
        print("PRESS CLASSIFICATION: {} (search: {})".format(display_name, press_search_name))
        print("=" * 80)

        raw_items = []
        if getattr(settings, "press_enable_google_news", True) and (google_news_search_phrases or press_search_name):
            try:
                gn = collect_google_news_items(
                    press_search_name,
                    max_items=min(50, max_per_source * 2),
                    window_days=window_days,
                    search_phrases=google_news_search_phrases,
                )
                raw_items.extend(gn)
                print("Google News: {} items".format(len(gn)))
            except Exception as e:
                print("Google News failed: {}".format(e))
        _prnewswire_skip: set[str] = set()  # No exclusions; full press for all including AKA, Landing, Rove.
        if (display_name or "").strip().lower() not in _prnewswire_skip:
            try:
                prn = collect_prnewswire_items(press_search_name, max_items=100, window_days=window_days)
                raw_items.extend(prn)
                print("PR Newswire: {} items".format(len(prn)))
            except Exception as e:
                print("PR Newswire failed: {}".format(e))
        else:
            print("PR Newswire skipped (ambiguous name: {})".format(display_name))

        filtered = []
        for it in raw_items:
            if (it.get("provider") or "").strip().lower() == "prnewswire":
                filtered.append(it)
                continue
            dt = _parse_press_date(it.get("date"))
            if dt and dt < cutoff:
                continue
            filtered.append(it)
        if len(filtered) > max_raw:
            filtered = filtered[:max_raw]
        print("After 90d + cap: {} items\n".format(len(filtered)))

        if not filtered:
            print("No items.")
            continue

        classified = get_press_classification_for_inspection(display_name, filtered, company_domains=[])

        n_included = sum(1 for it in classified if it.get("_included"))
        n_irrelevant = sum(1 for it in classified if (it.get("topic") or "").strip().lower() == "irrelevant")
        n_promo = sum(1 for it in classified if (it.get("topic") or "").strip().lower() == "promo_or_brand_marketing")
        print("Summary: {} included, {} irrelevant, {} promo (topic). {} dropped by filter.\n".format(
            n_included, n_irrelevant, n_promo, len(classified) - n_included,
        ))

        # Table header
        fmt = "{:4} | {:<22} | {:<24} | {}"
        print(fmt.format("#", "Status", "Topic", "Title (truncated)"))
        print("-" * 4 + "-+-" + "-" * 22 + "-+-" + "-" * 24 + "-+-" + "-" * 50)

        for i, it in enumerate(classified, 1):
            status = "INCLUDED" if it.get("_included") else "DROPPED:" + (it.get("_drop_reason") or "?")
            topic = (it.get("topic") or "").strip() or "—"
            title = (it.get("title") or "—").strip()
            if len(title) > 55:
                title = title[:52] + "..."
            print(fmt.format(i, status[:22], topic[:24], title[:50]))

        print("\nFull list (title | url | provider | topic | is_about_company | is_promo | status):")
        print("-" * 80)
        for i, it in enumerate(classified, 1):
            title = (it.get("title") or "—").strip()
            url = (it.get("url") or it.get("link") or "").strip()
            provider = (it.get("provider") or "").strip() or "—"
            topic = (it.get("topic") or "").strip() or "—"
            about = it.get("is_about_company", False)
            promo = it.get("is_promo", False)
            status = "INCLUDED" if it.get("_included") else "DROPPED:" + (it.get("_drop_reason") or "?")
            print("{} | {} | {} | {} | about={} promo={} | {}".format(
                i, title[:60], url[:70] if url else "—", provider, about, promo, status,
            ))
        print()

    print("Done.")


if __name__ == "__main__":
    main()
