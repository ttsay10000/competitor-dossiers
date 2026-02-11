#!/usr/bin/env python3
"""
Run the press pipeline for ONE competitor and print exact counts at each step:
  - How many articles are fetched (by source)
  - How many removed at each step (90d+cap, company-domain, classify, business filter)
  - Group LLM: N items -> M groups; final group count and article count

Usage:
  python scripts/test_press_pipeline_counts.py Lark
  python scripts/test_press_pipeline_counts.py AvantStay --local
  python scripts/test_press_pipeline_counts.py "Lark Hotels"

  With DB (default): uses competitor's press endpoints + Google News + PR Newswire, company_domains.
  --local: no DB; Google News + PR Newswire only for Lark/AvantStay/Placemakr; company_domains=[].

Requires: .env with DATABASE_URL (unless --local); OPENAI_API_KEY for enrichment.
"""
import io
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            import os
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _parse_press_date(value):
    if value is None:
        return None
    if hasattr(value, "year"):
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
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _normalize_domain(host: str) -> str:
    if not host:
        return ""
    h = (host or "").lower().strip()
    return h[4:] if h.startswith("www.") else h


LOCAL_COMPETITORS = [
    ("Lark", "Lark Hotels"),
    ("AvantStay", "AvantStay"),
    ("Placemakr", "Placemakr"),
]


def run_with_db(competitor_name: str, max_summarize: int):
    from app.db import get_session
    from app.models import Competitor
    from app.config import settings
    from app.collectors.press import collect_press_snapshot
    from app.collectors.global_press import collect_google_news_items, collect_prnewswire_items
    from app.llm_structured import enrich_press_items_with_llm

    with get_session() as session:
        competitors = session.query(Competitor).order_by(Competitor.name.asc()).all()
        matches = [c for c in competitors if (competitor_name or "").lower() in (c.name or "").lower()]
        if not matches:
            print(f"No competitor matching {competitor_name!r}. Use a substring of the competitor name.")
            return None, None, None
        competitor = matches[0]
        endpoints = [ep for ep in competitor.source_endpoints if ep.channel == "press"]
        press_search_name = competitor.name
        for ep in endpoints:
            opts = getattr(ep, "extra_options", None) if ep else None
            if isinstance(opts, dict) and opts.get("press_search_name"):
                press_search_name = (opts.get("press_search_name") or "").strip() or press_search_name
                break
        if (press_search_name or "").strip().lower() == "lark":
            press_search_name = "Lark Hotels"

        # --- Collection ---
        raw_items = []
        n_endpoint = 0
        for ep in endpoints:
            try:
                snapshot = collect_press_snapshot(ep.url)
            except Exception as e:
                print(f"  [press_endpoint] Error {ep.url}: {e}")
                continue
            for item in (snapshot.get("items") or []):
                if not isinstance(item, dict):
                    continue
                url = (item.get("url") or item.get("link") or "").strip()
                if url and not url.startswith("http"):
                    url = urljoin(snapshot.get("source_url") or ep.url, url)
                raw_items.append({
                    "title": item.get("title"),
                    "url": url,
                    "date": item.get("date"),
                    "source": item.get("source") or snapshot.get("source_url"),
                    "provider": "press_endpoint",
                })
            n_endpoint = sum(1 for it in raw_items if (it.get("provider") or "").strip() == "press_endpoint")

        n_gn = 0
        if getattr(settings, "press_enable_google_news", True) and press_search_name:
            try:
                gn_items = collect_google_news_items(
                    press_search_name,
                    max_items=min(50, getattr(settings, "press_max_items_per_source", 30) * 2),
                    window_days=90,
                )
                raw_items.extend(gn_items)
                n_gn = len(gn_items)
            except Exception as e:
                print(f"  Google News failed: {e}")

        n_pr = 0
        try:
            pr_items = collect_prnewswire_items(press_search_name, max_items=100, window_days=90)
            raw_items.extend(pr_items)
            n_pr = len(pr_items)
        except Exception as e:
            print(f"  PR Newswire failed: {e}")

        n_raw = len(raw_items)
        # 90d + cap
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        max_raw = getattr(settings, "press_max_raw_items_per_competitor", 120)
        filtered_items = []
        for item in raw_items:
            if (item.get("provider") or "").strip().lower() == "prnewswire":
                filtered_items.append(item)
                continue
            dt = _parse_press_date(item.get("date"))
            if dt is not None and dt < cutoff:
                continue
            filtered_items.append(item)
        n_after_90d = len(filtered_items)
        if n_after_90d > max_raw:
            filtered_items = filtered_items[:max_raw]
        n_after_cap = len(filtered_items)

        company_domains = []
        if getattr(competitor, "primary_domain", None):
            company_domains.append(competitor.primary_domain)
        for ep in endpoints:
            try:
                netloc = urlparse(ep.url).netloc
                if netloc:
                    company_domains.append(netloc)
            except Exception:
                pass
        company_domains = list({_normalize_domain(d) for d in company_domains if d})

        # Capture stderr during enrich
        stderr_capture = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = stderr_capture
        try:
            press_groups = enrich_press_items_with_llm(
                competitor.name,
                filtered_items,
                max_articles_to_summarize=max_summarize,
                company_domains=company_domains,
                previous_items=None,
                previous_canonical=None,
            )
        finally:
            sys.stderr = old_stderr

        canonical = [art for g in press_groups for art in (g.get("articles") or [])]
        stderr_text = stderr_capture.getvalue()
        return {
            "name": competitor.name,
            "press_search_name": press_search_name,
            "n_endpoint": n_endpoint,
            "n_google_news": n_gn,
            "n_pr_newswire": n_pr,
            "n_raw_total": n_raw,
            "n_after_90d": n_after_90d,
            "n_after_cap": n_after_cap,
            "n_final_canonical": len(canonical),
            "n_groups": len(press_groups),
        }, stderr_text, canonical


def run_local(competitor_name: str, max_summarize: int):
    from app.config import settings
    from app.collectors.global_press import collect_google_news_items, collect_prnewswire_items
    from app.llm_structured import enrich_press_items_with_llm

    name_lower = (competitor_name or "").strip().lower()
    matches = [(d, s) for d, s in LOCAL_COMPETITORS if name_lower in d.lower() or name_lower in s.lower()]
    if not matches:
        print(f"No local competitor matching {competitor_name!r}. Options: Lark, AvantStay, Placemakr.")
        return None, None, None
    display_name, press_search_name = matches[0]

    raw_items = []
    n_gn = 0
    if getattr(settings, "press_enable_google_news", True):
        try:
            gn_items = collect_google_news_items(
                press_search_name,
                max_items=min(50, 60),
                window_days=90,
            )
            raw_items.extend(gn_items)
            n_gn = len(gn_items)
        except Exception as e:
            print(f"  Google News failed: {e}")

    n_pr = 0
    try:
        pr_items = collect_prnewswire_items(press_search_name, max_items=100, window_days=90)
        raw_items.extend(pr_items)
        n_pr = len(pr_items)
    except Exception as e:
        print(f"  PR Newswire failed: {e}")

    n_raw = len(raw_items)
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    max_raw = getattr(settings, "press_max_raw_items_per_competitor", 120)
    filtered_items = []
    for item in raw_items:
        if (item.get("provider") or "").strip().lower() == "prnewswire":
            filtered_items.append(item)
            continue
        dt = _parse_press_date(item.get("date"))
        if dt is not None and dt < cutoff:
            continue
        filtered_items.append(item)
    n_after_90d = len(filtered_items)
    if n_after_90d > max_raw:
        filtered_items = filtered_items[:max_raw]
    n_after_cap = len(filtered_items)

    stderr_capture = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = stderr_capture
    try:
        press_groups = enrich_press_items_with_llm(
            display_name,
            filtered_items,
            max_articles_to_summarize=max_summarize,
            company_domains=[],
            previous_items=None,
            previous_canonical=None,
        )
    finally:
        sys.stderr = old_stderr

    canonical = [art for g in press_groups for art in (g.get("articles") or [])]
    stderr_text = stderr_capture.getvalue()
    return {
        "name": display_name,
        "press_search_name": press_search_name,
        "n_endpoint": 0,
        "n_google_news": n_gn,
        "n_pr_newswire": n_pr,
        "n_raw_total": n_raw,
        "n_after_90d": n_after_90d,
        "n_after_cap": n_after_cap,
        "n_final_canonical": len(canonical),
        "n_groups": len(press_groups),
    }, stderr_text, canonical


def parse_enrich_stderr(stderr_text: str) -> dict:
    """Extract step counts from [press] log lines."""
    out = {}
    for line in stderr_text.splitlines():
        line = line.strip()
        if "[press]" not in line:
            continue
        # Company-domain filter: N -> M items (X on own domain dropped)
        m = re.search(r"Company-domain filter: (\d+) -> (\d+) items \((\d+) on own domain dropped\)", line)
        if m:
            out["company_domain_in"] = int(m.group(1))
            out["company_domain_out"] = int(m.group(2))
            out["company_domain_dropped"] = int(m.group(3))
            continue
        # Classify: N items — irrelevant=X, promo_or_brand_marketing=Y, other=Z
        m = re.search(r"Classify: (\d+) items — irrelevant=(\d+), promo_or_brand_marketing=(\d+), other=(\d+)", line)
        if m:
            out["classify_total"] = int(m.group(1))
            out["classify_irrelevant"] = int(m.group(2))
            out["classify_promo"] = int(m.group(3))
            out["classify_other"] = int(m.group(4))
            continue
        # Business filter: N -> M items (X dropped). Grouping input: M items
        m = re.search(r"Business filter: (\d+) -> (\d+) items \((\d+) dropped\)\. Grouping input: (\d+) items", line)
        if m:
            out["business_filter_in"] = int(m.group(1))
            out["business_filter_out"] = int(m.group(2))
            out["business_filter_dropped"] = int(m.group(3))
            continue
        # Group LLM: N items -> M groups
        m = re.search(r"Group LLM: (\d+) items -> (\d+) groups", line)
        if m:
            out["group_llm_items"] = int(m.group(1))
            out["group_llm_groups"] = int(m.group(2))
            continue
        # Press pipeline done: N groups
        m = re.search(r"Press pipeline done: (\d+) groups", line)
        if m:
            out["pipeline_done_groups"] = int(m.group(1))
            continue
    return out


def main():
    args = [a for a in sys.argv[1:] if a and not a.startswith("-")]
    use_local = "--local" in sys.argv
    competitor_name = (args[0] or "Lark").strip()
    max_summarize = getattr(
        __import__("app.config", fromlist=["settings"]).settings,
        "press_max_articles_to_summarize",
        40,
    )

    print("=" * 70)
    print(f"PRESS PIPELINE COUNTS — {competitor_name}" + (" (local, no DB)" if use_local else " (with DB)"))
    print("=" * 70)

    if use_local:
        result, stderr_text, canonical = run_local(competitor_name, max_summarize)
    else:
        result, stderr_text, canonical = run_with_db(competitor_name, max_summarize)

    if result is None:
        return

    # --- Collection summary ---
    print("\n--- FETCHED ---")
    print(f"  User press endpoints:  {result['n_endpoint']} items")
    print(f"  Google News (90d):     {result['n_google_news']} items")
    print(f"  PR Newswire (90d):     {result['n_pr_newswire']} items")
    print(f"  Raw total:             {result['n_raw_total']} items")

    print("\n--- AFTER 90-DAY WINDOW + CAP ---")
    dropped_90d = result["n_raw_total"] - result["n_after_90d"]
    dropped_cap = max(0, result["n_after_90d"] - result["n_after_cap"])
    print(f"  After 90d window:      {result['n_after_90d']} items  (dropped {dropped_90d} older than 90 days)")
    print(f"  After cap:              {result['n_after_cap']} items  (cap dropped {dropped_cap})")
    print(f"  → Input to enrichment: {result['n_after_cap']} items")

    # --- Enrichment steps (from stderr) ---
    steps = parse_enrich_stderr(stderr_text)

    print("\n--- ENRICHMENT STEPS ---")
    if "company_domain_in" in steps:
        print(f"  Company-domain filter: {steps['company_domain_in']} → {steps['company_domain_out']}  (removed {steps['company_domain_dropped']} on own domain)")
    if "classify_total" in steps:
        print(f"  Classify (2.1):         {steps['classify_total']} items  (irrelevant={steps.get('classify_irrelevant', 0)}, promo={steps.get('classify_promo', 0)}, other={steps.get('classify_other', 0)})")
    if "business_filter_in" in steps:
        print(f"  Business filter (2.3):  {steps['business_filter_in']} → {steps['business_filter_out']}  (removed {steps['business_filter_dropped']})")
    if "group_llm_items" in steps:
        print(f"  Group LLM (2.5):        {steps['group_llm_items']} items → {steps['group_llm_groups']} groups")
    if "pipeline_done_groups" in steps:
        print(f"  Press pipeline done:    {steps['pipeline_done_groups']} groups")
    print(f"  → Final:                 {result.get('n_groups', 0)} groups, {result['n_final_canonical']} articles")

    # All articles are already filtered; no topic on flattened items in new shape
    display = canonical or []
    print(f"  → To display:            {len(display)} articles")

    print("\n" + "=" * 70)
    print("Done.")
    if stderr_text and "--verbose" in sys.argv:
        print("\n--- Raw [press] stderr ---")
        print(stderr_text)


if __name__ == "__main__":
    main()
