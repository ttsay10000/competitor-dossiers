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
             optional scroll_selector (scroll this element to bottom instead of clicking).
    """
    click_selector = options.get("click_selector")
    scroll_selector = options.get("scroll_selector")
    stop_when_gone = options.get("stop_when_selector_gone", True)
    wait_ms = options.get("wait_after_click_ms", 1500)
    max_clicks = options.get("max_clicks", 50)
    wait_sec = wait_ms / 1000.0

    for _ in range(max_clicks):
        if click_selector:
            try:
                btn = page.locator(click_selector).first
                if not btn.is_visible():
                    break
                btn.click()
            except Exception:
                break
            time.sleep(wait_sec)
            if stop_when_gone:
                try:
                    if not page.locator(click_selector).first.is_visible():
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
