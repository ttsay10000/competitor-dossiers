#!/usr/bin/env python3
"""
Fetch Lark career page with Playwright and run the same scroll logic as the talent
collector, logging heights, scroll position, and job-title count each round to see
why we cap at ~47 instead of 60-65.

Usage (from repo root; needs PLAYWRIGHT_ENABLED and playwright install):
  python3 scripts/debug_lark_scroll.py

Optional:
  --save-html   Write final HTML to debug_lark_scroll_output.html
  --container SELECTOR   Scroll this element instead of window (e.g. div with overflow)

Requires: playwright install  (then run with python3 scripts/debug_lark_scroll.py)
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
env_file = ROOT / ".env"
if env_file.exists():
    import os
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Same options as talent collector for WizeHire
LARK_TALENT_URL = "https://ats.wizehire.com/career-site/lark-hospitality"
SCROLL_OPTIONS = {
    "scroll_by_viewport": True,
    "max_scrolls": 100,
    "scroll_wait_sec": 1.0,
    "scroll_batch_wait_sec": 4.0,
    "scroll_no_progress_limit": 5,
    "post_load_wait_ms": 3000,
}


def count_job_title_divs(page) -> int:
    """Count divs with class containing jss83 (job title on Lark)."""
    try:
        return page.evaluate(
            """() => {
                const divs = document.querySelectorAll('div[class*="jss83"]');
                return divs.length;
            }"""
        )
    except Exception:
        return -1


def get_scroll_info(page) -> dict:
    try:
        return page.evaluate(
            """() => ({
                scrollY: window.scrollY,
                scrollHeight: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
                innerHeight: window.innerHeight,
                atBottom: window.scrollY + window.innerHeight >=
                    Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) - 2
            })"""
        )
    except Exception:
        return {}


def find_scrollable_containers(page) -> list:
    """Find elements that have overflow and are scrollable (scrollHeight > clientHeight)."""
    try:
        return page.evaluate(
            """() => {
                const out = [];
                const walk = (el) => {
                    if (!el || el.nodeType !== 1) return;
                    const style = window.getComputedStyle(el);
                    const ov = style.overflowY || style.overflow;
                    if (ov === 'auto' || ov === 'scroll' || ov === 'overlay') {
                        const sh = el.scrollHeight, ch = el.clientHeight;
                        if (sh > ch + 10) {
                            const sel = el.id ? '#' + el.id : el.className && typeof el.className === 'string'
                                ? '.' + el.className.trim().split(/\\s+/)[0] : null;
                            out.push({
                                tag: el.tagName,
                                id: el.id || '',
                                className: (el.className && typeof el.className === 'string' ? el.className : '').slice(0, 80),
                                scrollHeight: sh,
                                clientHeight: ch,
                                selector: sel || el.tagName.toLowerCase()
                            });
                        }
                    }
                    for (const c of el.children) walk(c);
                };
                walk(document.body);
                return out;
            }"""
        )
    except Exception:
        return []


def scroll_page_to_exhaust_debug(page, options, log_round):
    """Same logic as http.scroll_page_to_exhaust but with per-round logging."""
    max_scrolls = options.get("max_scrolls", 100)
    scroll_wait_sec = options.get("scroll_wait_sec", 1.0)
    no_progress_limit = options.get("scroll_no_progress_limit", 5)
    batch_wait_sec = options.get("scroll_batch_wait_sec", 4.0)
    scroll_by_viewport = options.get("scroll_by_viewport", False)
    container_sel = options.get("scroll_container_selector")
    wait_after_bottom = batch_wait_sec if batch_wait_sec > 0 else scroll_wait_sec

    if container_sel:
        def get_height():
            return page.evaluate(
                """(sel) => { const el = document.querySelector(sel); return el ? el.scrollHeight : 0; }""",
                container_sel,
            )

        def at_bottom():
            return page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    return !el || (el.scrollTop + el.clientHeight >= el.scrollHeight - 2);
                }""",
                container_sel,
            )

        def do_scroll_step():
            page.evaluate(
                """(sel) => { const el = document.querySelector(sel); if (el) el.scrollTop = el.scrollHeight; }""",
                container_sel,
            )

        def do_scroll_by_viewport():
            page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (el) el.scrollTop = Math.min(el.scrollTop + el.clientHeight * 0.85, el.scrollHeight);
                }""",
                container_sel,
            )
    else:
        def get_height():
            return page.evaluate(
                "() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
            )

        def at_bottom():
            return page.evaluate(
                "() => window.scrollY + window.innerHeight >= "
                "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) - 2"
            )

        def do_scroll_step():
            page.evaluate(
                "() => window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight))"
            )

        def do_scroll_by_viewport():
            page.evaluate("() => { const step = window.innerHeight * 0.85; window.scrollBy(0, step); }")

    prev_height = -1
    scrolls = 0
    no_progress = 0

    for round_num in range(max_scrolls):
        try:
            if scroll_by_viewport:
                for _ in range(50):
                    if at_bottom():
                        break
                    do_scroll_by_viewport()
                    time.sleep(0.4)
                do_scroll_step()
                time.sleep(0.3)
                time.sleep(wait_after_bottom)
            else:
                do_scroll_step()
                time.sleep(scroll_wait_sec)
        except Exception as e:
            log_round(round_num, None, None, error=str(e))
            break
        scrolls += 1
        try:
            new_height = get_height()
            info = get_scroll_info(page)
            job_count = count_job_title_divs(page)
        except Exception as e:
            log_round(round_num, None, None, error=str(e))
            break

        log_round(round_num, new_height, job_count, scroll_info=info)

        if new_height == prev_height:
            no_progress += 1
            if no_progress >= no_progress_limit or at_bottom():
                print(f"  -> Stopping: no_progress={no_progress}, at_bottom={at_bottom()}")
                break
        else:
            no_progress = 0
        prev_height = new_height

    return scrolls


def main():
    save_html = "--save-html" in sys.argv
    container_selector = None
    for i, arg in enumerate(sys.argv):
        if arg == "--container" and i + 1 < len(sys.argv):
            container_selector = sys.argv[i + 1]
            break
    if container_selector:
        SCROLL_OPTIONS["scroll_container_selector"] = container_selector
        print(f"Using scroll container: {container_selector!r}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install playwright && python -m playwright install chromium")
        sys.exit(1)

    def log_round(round_num, height, job_count, scroll_info=None, error=None):
        if error:
            print(f"  Round {round_num}: ERROR {error}")
            return
        info = scroll_info or {}
        at_b = info.get("atBottom", "?")
        print(
            f"  Round {round_num}: height={height} scrollY={info.get('scrollY')} "
            f"innerHeight={info.get('innerHeight')} atBottom={at_b} job_divs(jss83)={job_count}"
        )

    print("Fetching Lark career page...")
    print(f"  URL: {LARK_TALENT_URL}")
    print(f"  post_load_wait_ms: {SCROLL_OPTIONS.get('post_load_wait_ms', 0)}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(LARK_TALENT_URL, wait_until="networkidle", timeout=60000)
        post_ms = SCROLL_OPTIONS.get("post_load_wait_ms", 0)
        if post_ms > 0:
            print(f"  Waiting {post_ms}ms for initial content...")
            time.sleep(post_ms / 1000.0)

        info = get_scroll_info(page)
        job_count_initial = count_job_title_divs(page)
        print(f"  Initial: height={info.get('scrollHeight')} job_divs(jss83)={job_count_initial}")

        # Detect scrollable containers (list may be in a div, not window)
        containers = find_scrollable_containers(page)
        if containers:
            print(f"\n  Scrollable containers found ({len(containers)}):")
            for c in containers[:15]:
                sel = c.get("selector") or f"tag={c.get('tag')}"
                print(f"    {sel}  scrollHeight={c.get('scrollHeight')} clientHeight={c.get('clientHeight')}  class={c.get('className', '')[:50]!r}")
            if len(containers) > 15:
                print(f"    ... and {len(containers) - 15} more")
            print("  If job list is in one of these, try: --container \"SELECTOR\" (e.g. .className or #id)")
        print()

        print("Scrolling (viewport steps + batch wait)...")
        scrolls = scroll_page_to_exhaust_debug(page, SCROLL_OPTIONS, log_round)
        print(f"\nTotal rounds: {scrolls}")
        print()

        final_info = get_scroll_info(page)
        final_job_divs = count_job_title_divs(page)
        print(f"Final: height={final_info.get('scrollHeight')} job_divs(jss83)={final_job_divs}")

        html = page.content()
        browser.close()

    # Run our actual extractor on the HTML
    from app.collectors.talent import extract_jobs_from_wizehire_title_divs

    jobs = extract_jobs_from_wizehire_title_divs(html)
    print(f"Extracted jobs (extract_jobs_from_wizehire_title_divs): {len(jobs)}")
    if jobs:
        for i, j in enumerate(jobs[:10]):
            print(f"  [{i}] {j.get('title')}")
        if len(jobs) > 10:
            print(f"  ... and {len(jobs) - 10} more")

    if save_html:
        out_path = ROOT / "debug_lark_scroll_output.html"
        out_path.write_text(html, encoding="utf-8")
        print(f"\nSaved HTML to {out_path}")

    print()
    if len(jobs) < 60:
        print("If job count is still ~47:")
        print("  1. Check 'Scrollable containers found' above; if one holds the job list, re-run with:")
        print("     python3 scripts/debug_lark_scroll.py --container \"SELECTOR\"")
        print("  2. Add scroll_container_selector to talent scroll_options in app/collectors/talent.py")
        print("  3. Or the site may use a different class (e.g. not jss83) for later batches.")


if __name__ == "__main__":
    main()
