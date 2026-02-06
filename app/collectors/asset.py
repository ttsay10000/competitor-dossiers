import re
import gzip
from datetime import datetime
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from .http import fetch_url, fetch_url_js


def discover_sitemap(url: str) -> list[str]:
    if url.endswith("/"):
        base = url[:-1]
    else:
        base = url
    return [f"{base}/sitemap.xml", f"{base}/sitemap.xml.gz"]


def extract_links_from_sitemap(xml_text: str) -> list[str]:
    soup = BeautifulSoup(xml_text, "xml")
    urls = []
    for loc in soup.find_all("loc"):
        if loc.text:
            urls.append(loc.text.strip())
    return urls


def expand_sitemap(url: str) -> list[str]:
    fetched = fetch_url(url)
    if fetched.status_code != 200:
        return []

    if url.endswith(".gz"):
        try:
            xml_text = gzip.decompress(fetched.text.encode("utf-8")).decode("utf-8")
        except Exception:
            return []
    else:
        xml_text = fetched.text

    urls = extract_links_from_sitemap(xml_text)
    return urls


def is_property_like(url: str) -> bool:
    patterns = [
        r"/properties/",
        r"/property/",
        r"/locations/",
        r"/apartments/",
        r"/homes/",
        r"/destinations/",
        r"/search",
    ]
    return any(re.search(pattern, url) for pattern in patterns)


def extract_properties_from_html(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    properties = []
    for link in soup.find_all("a"):
        href = link.get("href") or ""
        text = (link.get_text() or "").strip()
        if not href or len(text) < 3:
            continue
        if not is_property_like(href):
            continue
        properties.append(
            {
                "url": href,
                "name": text,
                "market": None,
                "status": None,
            }
        )
    return properties


def normalize_properties(properties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for prop in properties:
        normalized.append(
            {
                "url": prop.get("url"),
                "name": (prop.get("name") or "").strip(),
                "market": (prop.get("market") or "").strip() or None,
                "status": (prop.get("status") or "").strip() or None,
            }
        )
    return normalized


def collect_asset_snapshot(
    source_url: str,
    js_required: bool = False,
    use_sitemap_first: bool = False,
) -> dict[str, Any]:
    if js_required:
        fetched = fetch_url_js(source_url)
        properties = extract_properties_from_html(fetched.text)
        return {
            "source_url": fetched.url,
            "raw_content": fetched.text,
            "raw_hash": fetched.raw_hash,
            "properties": normalize_properties(properties),
            "note": "js_rendered",
        }

    def fetch_from_sitemap() -> Optional[Dict[str, Any]]:
        for sitemap_url in discover_sitemap(source_url):
            urls = expand_sitemap(sitemap_url)
            if not urls:
                continue
            property_urls = [url for url in urls if is_property_like(url)]
            return {
                "source_url": sitemap_url,
                "raw_content": None,
                "raw_hash": None,
                "properties": normalize_properties([{"url": url, "name": url} for url in property_urls]),
            }
        return None

    def fetch_from_html() -> dict[str, Any]:
        fetched = fetch_url(source_url)
        properties = extract_properties_from_html(fetched.text)
        return {
            "source_url": fetched.url,
            "raw_content": fetched.text,
            "raw_hash": fetched.raw_hash,
            "properties": normalize_properties(properties),
        }

    if use_sitemap_first:
        sitemap_snapshot = fetch_from_sitemap()
        if sitemap_snapshot:
            return sitemap_snapshot
        return fetch_from_html()

    html_snapshot = fetch_from_html()
    if html_snapshot.get("properties"):
        return html_snapshot
    sitemap_snapshot = fetch_from_sitemap()
    return sitemap_snapshot or html_snapshot


def build_structured_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_url": snapshot.get("source_url"),
        "fetched_at": datetime.utcnow().isoformat(),
        "properties": snapshot.get("properties", []),
        "note": snapshot.get("note"),
    }
