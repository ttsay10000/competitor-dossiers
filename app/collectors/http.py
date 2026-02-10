import hashlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


@dataclass
class FetchResult:
    url: str
    status_code: int
    content_type: Optional[str]
    text: str
    raw_hash: str


def fetch_url(url: str, timeout: int = 20) -> FetchResult:
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "competitor-signals/0.1"})
    content_type = response.headers.get("content-type")
    text = response.text or ""
    raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return FetchResult(
        url=response.url,
        status_code=response.status_code,
        content_type=content_type,
        text=text,
        raw_hash=raw_hash,
    )


def fetch_url_js(url: str) -> FetchResult:
    from ..config import settings
    if not settings.playwright_enabled:
        raise RuntimeError("playwright is disabled; set PLAYWRIGHT_ENABLED=true")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright is not installed")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        text = page.content()
        browser.close()

    raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return FetchResult(
        url=url,
        status_code=200,
        content_type="text/html",
        text=text,
        raw_hash=raw_hash,
    )


def exhaust_list_in_browser(page: Any, options: Dict[str, Any]) -> None:
    """
    Click 'Load more' or scroll until the trigger is gone or max iterations.
    Mutates the page DOM by triggering loading; caller should then call page.content().
    options: click_selector, stop_when_selector_gone (default True), wait_after_click_ms (1500), max_clicks (50),
             wait_for_selector_timeout_ms (8000), wait_after_gone_ms (2500),
             optional scroll_selector (scroll this element to bottom instead of clicking).
    """
    click_selector = options.get("click_selector")
    scroll_selector = options.get("scroll_selector")
    stop_when_gone = options.get("stop_when_selector_gone", True)
    wait_ms = options.get("wait_after_click_ms", 1500)
    max_clicks = options.get("max_clicks", 50)
    wait_for_timeout_ms = options.get("wait_for_selector_timeout_ms", 8000)
    wait_after_gone_ms = options.get("wait_after_gone_ms", 2500)
    wait_sec = wait_ms / 1000.0

    # Support multiple selectors (e.g. "Load more" / "Load More" / "View more"); use first that appears
    selectors = click_selector if isinstance(click_selector, list) else [click_selector] if click_selector else []

    if selectors:
        # Wait for any of the buttons to appear (page may load list + button after networkidle)
        try:
            for sel in selectors:
                page.locator(sel).first.wait_for(state="visible", timeout=wait_for_timeout_ms)
                break
        except Exception:
            pass  # None visible yet; loop will run and may find it or return current content

    for _ in range(max_clicks):
        if selectors:
            btn = None
            for sel in selectors:
                loc = page.locator(sel).first
                try:
                    if loc.is_visible():
                        btn = loc
                        break
                except Exception:
                    continue
            if not btn:
                break
            try:
                btn.scroll_into_view_if_needed()
                btn.click()
            except Exception:
                break
            time.sleep(wait_sec)
            if stop_when_gone:
                try:
                    visible = False
                    for sel in selectors:
                        try:
                            if page.locator(sel).first.is_visible():
                                visible = True
                                break
                        except Exception:
                            continue
                    if not visible:
                        # Button may be temporarily hidden (e.g. "Loading..."); wait and recheck
                        time.sleep(wait_after_gone_ms / 1000.0)
                        visible_after = False
                        for sel in selectors:
                            try:
                                if page.locator(sel).first.is_visible():
                                    visible_after = True
                                    break
                            except Exception:
                                continue
                        if not visible_after:
                            break
                except Exception:
                    break
        elif scroll_selector:
            try:
                el = page.locator(scroll_selector).first
                el.evaluate("el => el.scrollTop = el.scrollHeight")
            except Exception:
                break
            time.sleep(wait_sec)
        else:
            break


def fetch_url_js_exhaust(url: str, load_more_options: Dict[str, Any]) -> FetchResult:
    """Load URL with Playwright, run exhaust_list_in_browser, return final HTML."""
    from ..config import settings
    if not settings.playwright_enabled:
        raise RuntimeError("playwright is disabled; set PLAYWRIGHT_ENABLED=true")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright is not installed")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        exhaust_list_in_browser(page, load_more_options)
        text = page.content()
        browser.close()

    raw_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return FetchResult(
        url=url,
        status_code=200,
        content_type="text/html",
        text=text,
        raw_hash=raw_hash,
    )
