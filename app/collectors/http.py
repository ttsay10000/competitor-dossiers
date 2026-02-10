import hashlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

# Default UA for general crawling.
USER_AGENT_DEFAULT = "competitor-signals/0.1"
# Browser-like UA to reduce "Please enable JS" / ad-block walls that only check User-Agent.
USER_AGENT_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class FetchResult:
    url: str
    status_code: int
    content_type: Optional[str]
    text: str
    raw_hash: str


def fetch_url(url: str, timeout: int = 20, headers: Optional[Dict[str, str]] = None) -> FetchResult:
    h = headers if headers is not None else {"User-Agent": USER_AGENT_DEFAULT}
    if "User-Agent" not in h and headers is not None:
        h = {**h, "User-Agent": USER_AGENT_DEFAULT}
    elif headers is None:
        h = {"User-Agent": USER_AGENT_DEFAULT}
    response = requests.get(url, timeout=timeout, headers=h)
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


def _button_visible(page: Any, selectors: list) -> bool:
    """True if any of the click selectors is visible."""
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible():
                return True
        except Exception:
            continue
    return False


def _find_button_fallback(page: Any, button_text: str):
    """Try Playwright get_by_role / get_by_text (often more reliable for dynamic content)."""
    if not button_text:
        return None
    try:
        loc = page.get_by_role("link", name=button_text)
        if loc.count() > 0 and loc.first.is_visible():
            return loc.first
    except Exception:
        pass
    try:
        loc = page.get_by_text(button_text, exact=False)
        if loc.count() > 0 and loc.first.is_visible():
            return loc.first
    except Exception:
        pass
    return None


def exhaust_list_in_browser(page: Any, options: Dict[str, Any]) -> int:
    """
    Click 'Load more' (or equivalent) until the trigger is gone or max iterations.
    Mutates the page DOM by triggering loading; caller should then call page.content().
    options: click_selector, stop_when_selector_gone (default True), wait_after_click_ms (1500), max_clicks (50),
             wait_for_selector_timeout_ms (8000), wait_after_gone_ms (2500), wait_reappear_attempts (3),
             button_text (optional): fallback via get_by_role/get_by_text e.g. "Load more hotels",
             optional scroll_selector (scroll this element to bottom instead of clicking).
    Returns the number of clicks performed.
    """
    click_selector = options.get("click_selector")
    scroll_selector = options.get("scroll_selector")
    button_text = (options.get("button_text") or "").strip()  # fallback for get_by_role/get_by_text
    stop_when_gone = options.get("stop_when_selector_gone", True)
    wait_ms = options.get("wait_after_click_ms", 1500)
    max_clicks = options.get("max_clicks", 50)
    wait_for_timeout_ms = options.get("wait_for_selector_timeout_ms", 8000)
    wait_after_gone_ms = options.get("wait_after_gone_ms", 2500)
    reappear_attempts = options.get("wait_reappear_attempts", 3)  # recheck multiple times before assuming "done"
    wait_sec = wait_ms / 1000.0

    # Support multiple selectors (e.g. "Load more hotels" / "Load more" / "View more"); use first that appears
    selectors = click_selector if isinstance(click_selector, list) else [click_selector] if click_selector else []
    clicks = 0
    use_text_fallback = bool(button_text)

    if selectors:
        # Wait for any of the buttons to appear (page may load list + button after networkidle)
        try:
            for sel in selectors:
                page.locator(sel).first.wait_for(state="visible", timeout=wait_for_timeout_ms)
                break
        except Exception:
            pass  # None visible yet; loop will run and may find it or return current content

    for _ in range(max_clicks):
        if selectors or use_text_fallback:
            btn = None
            if use_text_fallback:
                btn = _find_button_fallback(page, button_text)
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
                break
            try:
                btn.scroll_into_view_if_needed()
                btn.click()
                clicks += 1
            except Exception:
                break
            time.sleep(wait_sec)
            if stop_when_gone:
                try:
                    still_visible = (use_text_fallback and _find_button_fallback(page, button_text)) or (selectors and _button_visible(page, selectors))
                    if not still_visible:
                        # Button may be temporarily hidden (e.g. "Loading..."); wait and recheck several times
                        for attempt in range(reappear_attempts):
                            time.sleep(wait_after_gone_ms / 1000.0)
                            if (use_text_fallback and _find_button_fallback(page, button_text)) or (selectors and _button_visible(page, selectors)):
                                break
                        else:
                            # Still not visible after all rechecks -> list exhausted
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

    return clicks


def fetch_url_js_exhaust(url: str, load_more_options: Dict[str, Any]) -> FetchResult:
    """Load URL with Playwright, run exhaust_list_in_browser, return final HTML."""
    from ..config import settings
    if not settings.playwright_enabled:
        raise RuntimeError("playwright is disabled; set PLAYWRIGHT_ENABLED=true")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright is not installed")

    post_wait_ms = load_more_options.get("post_load_wait_ms", 0)
    post_wait_sec = post_wait_ms / 1000.0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        if post_wait_sec > 0:
            time.sleep(post_wait_sec)
        clicks = exhaust_list_in_browser(page, load_more_options)
        if (load_more_options.get("click_selector") or load_more_options.get("button_text")) and clicks >= 0:
            print(f"[load_more] {url[:60]}... -> {clicks} click(s)")
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
