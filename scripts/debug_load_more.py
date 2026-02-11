#!/usr/bin/env python3
"""
Debug the "Load more" / "Load more hotels" flow for Lark (or any URL) using Playwright.
Run locally to verify the button is found and clicks work. No DB required.

Usage (from repo root). Run ONE command at a time (do not paste the whole block):
  PLAYWRIGHT_ENABLED=true python3 scripts/debug_load_more.py
  PLAYWRIGHT_ENABLED=true HEADED=1 python3 scripts/debug_load_more.py
  PLAYWRIGHT_ENABLED=true WAIT_AFTER_LOAD=5 python3 scripts/debug_load_more.py
  PLAYWRIGHT_ENABLED=true python3 scripts/debug_load_more.py --save-html
If python3 is missing, use: .venv/bin/python scripts/debug_load_more.py
Requires: playwright installed, Chromium: python3 -m playwright install chromium
"""
import argparse
import os
import sys
import time

# Run from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LARK_PORTFOLIO_URL = "https://www.larkhospitality.com/portfolio/"

# Same options as seed.py for Lark (button_text + post_load_wait help on slow/fragile pages)
LARK_LOAD_MORE = {
    "button_text": "Load more hotels",
    "post_load_wait_ms": 3000,
    "click_selector": [
        "a:has-text('Load more hotels')",
        "button:has-text('Load more hotels')",
        ":text('Load more hotels')",
        "button:has-text('Load more')",
        "a:has-text('Load more')",
        "button:has-text('View more')",
        "a:has-text('View more')",
    ],
    "stop_when_selector_gone": True,
    "wait_after_click_ms": 2000,
    "wait_for_selector_timeout_ms": 15000,
    "wait_after_gone_ms": 3000,
    "wait_reappear_attempts": 5,
    "max_clicks": 200,
}


def _button_visible(page, selectors: list) -> bool:
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible():
                return True
        except Exception:
            continue
    return False


def _find_button_playwright_api(page, text: str):
    """Try Playwright's get_by_text / get_by_role (more resilient for dynamic content)."""
    try:
        # Link with exact text (Lark uses "Load more hotels" as link text)
        loc = page.get_by_role("link", name=text)
        if loc.count() > 0 and loc.first.is_visible():
            return loc.first
    except Exception:
        pass
    try:
        loc = page.get_by_text(text, exact=False)
        if loc.count() > 0 and loc.first.is_visible():
            return loc.first
    except Exception:
        pass
    return None


def exhaust_with_logging(page, options: dict, verbose: bool = True) -> int:
    """Same logic as http.exhaust_list_in_browser but with print statements."""
    click_selector = options.get("click_selector")
    selectors = click_selector if isinstance(click_selector, list) else [click_selector] if click_selector else []
    stop_when_gone = options.get("stop_when_selector_gone", True)
    wait_ms = options.get("wait_after_click_ms", 1500)
    max_clicks = options.get("max_clicks", 50)
    wait_for_timeout_ms = options.get("wait_for_selector_timeout_ms", 8000)
    wait_after_gone_ms = options.get("wait_after_gone_ms", 2500)
    reappear_attempts = options.get("wait_reappear_attempts", 3)
    wait_sec = wait_ms / 1000.0
    clicks = 0

    # 1) Try Playwright API first (get_by_role / get_by_text) — often more reliable
    button_text = "Load more hotels"
    btn = _find_button_playwright_api(page, button_text)
    use_playwright_api = btn is not None
    if use_playwright_api and verbose:
        print(f"  [debug] Found button via get_by_role/get_by_text({button_text!r})")

    if not use_playwright_api and selectors:
        if verbose:
            print(f"  [debug] Waiting up to {wait_for_timeout_ms}ms for one of {len(selectors)} selector(s)...")
        try:
            for sel in selectors:
                page.locator(sel).first.wait_for(state="visible", timeout=wait_for_timeout_ms)
                if verbose:
                    print(f"  [debug] Found with selector: {sel[:60]}...")
                break
        except Exception as e:
            if verbose:
                print(f"  [debug] No selector matched within timeout: {e}")

    for round in range(max_clicks):
        btn = None
        if use_playwright_api:
            btn = _find_button_playwright_api(page, button_text)
        if not btn and selectors:
            for sel in selectors:
                loc = page.locator(sel).first
                try:
                    if loc.is_visible():
                        btn = loc
                        break
                except Exception:
                    continue
        if not btn:
            if verbose and round == 0:
                print("  [debug] No button visible on first round — check selectors or run with HEADED=1 to watch.")
            break
        try:
            btn.scroll_into_view_if_needed()
            btn.click()
            clicks += 1
            if verbose:
                print(f"  [debug] Click #{clicks}")
        except Exception as e:
            if verbose:
                print(f"  [debug] Click failed: {e}")
            break
        time.sleep(wait_sec)
        if stop_when_gone:
            if not (_button_visible(page, selectors) if selectors else False) and not (use_playwright_api and _find_button_playwright_api(page, button_text)):
                for attempt in range(reappear_attempts):
                    time.sleep(wait_after_gone_ms / 1000.0)
                    if selectors and _button_visible(page, selectors):
                        break
                    if use_playwright_api and _find_button_playwright_api(page, button_text):
                        break
                else:
                    if verbose:
                        print(f"  [debug] Button did not reappear after {reappear_attempts} rechecks — list exhausted.")
                    break
    return clicks


def main() -> None:
    parser = argparse.ArgumentParser(description="Debug Playwright Load more for Lark (or custom URL)")
    parser.add_argument("--url", default=LARK_PORTFOLIO_URL, help="Page URL")
    parser.add_argument("--save-html", action="store_true", help="Save final HTML to debug_load_more_output.html")
    parser.add_argument("--no-verbose", action="store_true", help="Less logging")
    args = parser.parse_args()

    if os.environ.get("PLAYWRIGHT_ENABLED", "").lower() not in ("1", "true", "yes"):
        print("Set PLAYWRIGHT_ENABLED=true to run.")
        sys.exit(1)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install playwright && python -m playwright install chromium")
        sys.exit(1)

    wait_after_load = float(os.environ.get("WAIT_AFTER_LOAD", "3"))
    headed = os.environ.get("HEADED", "").lower() in ("1", "true", "yes")
    verbose = not args.no_verbose

    url = args.url
    print("Debug Load more")
    print("  URL:", url)
    print("  Headed:", headed)
    print("  Wait after page load: %.1fs" % wait_after_load)
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page()
        if verbose:
            print("  Loading page (networkidle, 60s timeout)...")
        page.goto(url, wait_until="networkidle", timeout=60000)
        len_before = len(page.content())
        if verbose:
            print("  Initial HTML length: %d chars" % len_before)
        if wait_after_load > 0:
            if verbose:
                print("  Waiting %.1fs for dynamic content..." % wait_after_load)
            time.sleep(wait_after_load)
        clicks = exhaust_with_logging(page, LARK_LOAD_MORE, verbose=verbose)
        html = page.content()
        browser.close()

    len_after = len(html)
    print()
    print("Result: %d click(s) | HTML before: %d -> after: %d chars" % (clicks, len_before, len_after))
    if args.save_html:
        out_path = "debug_load_more_output.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print("Saved: %s" % out_path)
    if clicks == 0:
        print("Tip: Run with HEADED=1 to watch the browser; or increase WAIT_AFTER_LOAD (e.g. 5 or 10).")
    sys.exit(0 if clicks > 0 else 1)


if __name__ == "__main__":
    main()
