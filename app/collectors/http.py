import hashlib
from dataclasses import dataclass
from typing import Optional

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
