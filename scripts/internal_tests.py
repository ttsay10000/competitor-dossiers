#!/usr/bin/env python3
"""
Internal tests for asset, press, and talent collectors.
Single entry point for all collector debug/test scripts.

To remove internal testing from the codebase: delete this file and the scripts
it invokes (debug_talent_llm_input, check_lark_llm_payload, sample_jobs_llm_payload,
debug_lark_scroll, debug_load_more, test_all_asset_collectors, test_lark_asset_fetch,
test_avantstay_asset_fetch, test_article_fetch, test_press_final_output,
test_press_compact_dedupe, debug_prnewswire_date, inspect_google_news_classify,
debug_dossier, debug_property_breakdown, debug_llm_location_clean).

Usage (from repo root):
  python scripts/internal_tests.py <channel> <test> [options]

Channels and tests:
  talent llm-input [competitor]     Inspect LLM payload + rule/Other counts (DB; optional OPENAI_API_KEY)
  talent lark-payload                Fetch Lark career page, show payload sent to LLM (no DB)
  talent sample-payload [competitor] Sample job dicts + LLM payload preview (DB)
  talent lark-scroll [--save-html] [--container SEL]  Debug Lark infinite scroll + job count

  asset load-more [--url URL] [--save-html]  Debug "Load more" for Lark portfolio (Playwright)
  asset all [--competitor NAME]               Run all asset collectors with diagnostics
  asset lark                                 Test Lark asset fetch (Playwright + load more)
  asset avantstay                            Test AvantStay asset fetch
  asset article-fetch                        Test article body fetch

  press final-output [competitor] [--no-enrich] [--local]  Full press pipeline, print canonical links
  press compact-dedupe [competitor]          Test compact dedupe payload
  press prnewswire-date                      Debug PR Newswire date parsing
  press google-news-classify                 Inspect Google News classification

  dossier                                    Debug dossier build (from DB)
  property-breakdown [competitor]            Debug property breakdown
  llm-location-clean [competitor]            Debug LLM location cleaning

Requires: .env (DATABASE_URL, optional OPENAI_API_KEY); playwright install for JS tests.
"""

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent

# Load .env
_env_file = ROOT / ".env"
if _env_file.exists():
    import os
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Map (channel, test) -> script path and argv override
DISPATCH = {
    ("talent", "llm-input"): ("debug_talent_llm_input.py", ["competitor"]),
    ("talent", "lark-payload"): ("check_lark_llm_payload.py", []),
    ("talent", "sample-payload"): ("sample_jobs_llm_payload.py", ["competitor"]),
    ("talent", "lark-scroll"): ("debug_lark_scroll.py", ["--save-html", "--container"]),
    ("asset", "load-more"): ("debug_load_more.py", ["--url", "--save-html", "--no-verbose"]),
    ("asset", "all"): ("test_all_asset_collectors.py", []),
    ("asset", "lark"): ("test_lark_asset_fetch.py", []),
    ("asset", "avantstay"): ("test_avantstay_asset_fetch.py", []),
    ("asset", "article-fetch"): ("test_article_fetch.py", []),
    ("press", "final-output"): ("test_press_final_output.py", ["--no-enrich", "--local", "competitor"]),
    ("press", "compact-dedupe"): ("test_press_compact_dedupe.py", []),
    ("press", "prnewswire-date"): ("debug_prnewswire_date.py", []),
    ("press", "google-news-classify"): ("inspect_google_news_classify.py", []),
    ("dossier", "dossier"): ("debug_dossier.py", []),
    ("property-breakdown", "property-breakdown"): ("debug_property_breakdown.py", ["competitor"]),
    ("llm-location-clean", "llm-location-clean"): ("debug_llm_location_clean.py", ["competitor"]),
}


def _run_script(script_name: str, extra_argv: list) -> None:
    script_path = SCRIPTS / script_name
    if not script_path.exists():
        print(f"Script not found: {script_path}", file=sys.stderr)
        sys.exit(1)
    sys.path.insert(0, str(ROOT))
    old_argv = sys.argv
    sys.argv = [script_name] + extra_argv
    try:
        runpy.run_path(str(script_path), run_name="__main__")
    finally:
        sys.argv = old_argv


def main() -> None:
    # Allow "internal_tests.py dossier" or "internal_tests.py property-breakdown Placemakr"
    if len(sys.argv) < 2:
        print(__doc__.strip())
        print("\nExample: python scripts/internal_tests.py talent llm-input Lark")
        sys.exit(1)

    channel = sys.argv[1].lower()
    # Commands that are just channel name: dossier, property-breakdown, llm-location-clean
    if channel in ("dossier", "property-breakdown", "llm-location-clean"):
        test = channel
        rest = sys.argv[2:]  # e.g. "Placemakr" for property-breakdown
    else:
        if len(sys.argv) < 3:
            print(__doc__.strip())
            print("\nExample: python scripts/internal_tests.py talent llm-input Lark")
            sys.exit(1)
        test = sys.argv[2].lower()
        rest = sys.argv[3:]

    key = (channel, test)
    if key not in DISPATCH:
        print(f"Unknown test: {channel} {test}", file=sys.stderr)
        print("Known: talent (llm-input, lark-payload, sample-payload, lark-scroll); "
              "asset (load-more, all, lark, avantstay, article-fetch); "
              "press (final-output, compact-dedupe, prnewswire-date, google-news-classify); "
              "dossier; property-breakdown; llm-location-clean", file=sys.stderr)
        sys.exit(1)

    script_name, known_opts = DISPATCH[key]
    # Pass through all remaining args (e.g. competitor name, --save-html, --container SEL)
    _run_script(script_name, rest)


if __name__ == "__main__":
    main()
