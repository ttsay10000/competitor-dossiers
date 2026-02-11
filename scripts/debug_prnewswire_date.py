#!/usr/bin/env python3
"""Fetch a PR Newswire article and print where dates appear: meta, JSON-LD, body."""
import os
import re
import sys

# Run from project root so app is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.collectors.http import fetch_url, USER_AGENT_BROWSER
from app.collectors.global_press import _date_from_article_html

URL = "https://www.prnewswire.com/news-releases/medtronic-earns-us-fda-approval-for-the-worlds-first-adaptive-deep-brain-stimulation-system-for-people-with-parkinsons-302382890.html"

def main():
    print("Fetching:", URL)
    resp = fetch_url(URL, timeout=20, headers={"User-Agent": USER_AGENT_BROWSER})
    print("Status:", resp.status_code)
    print("Body length:", len(resp.text or ""))
    html = resp.text or ""

    # Test the collector's date extractor
    parsed = _date_from_article_html(html)
    print("\n--- _date_from_article_html() result ---")
    print("  Parsed date:", parsed)
    print("  ISO for pipeline:", parsed.isoformat() if parsed else None)

    # 1) All meta tags that might be date-related
    print("\n--- META TAGS (property/content) ---")
    for m in re.finditer(r'<meta\s+([^>]+)>', html, re.IGNORECASE):
        tag = m.group(1)
        if "date" in tag.lower() or "published" in tag.lower() or "time" in tag.lower() or "article" in tag.lower():
            print("  ", m.group(0)[:200])

    # 2) JSON-LD blocks
    print("\n--- JSON-LD (application/ld+json) ---")
    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>([^<]+)</script>', html, re.IGNORECASE | re.DOTALL):
        block = m.group(1).strip()[:1200]
        if "date" in block.lower() or "published" in block.lower():
            print(block)
            print("  ---")

    # 3) datePublished / article:published_time in raw HTML
    print("\n--- datePublished or article:published_time in HTML ---")
    for m in re.finditer(r'.{0,30}(datePublished|article:published_time|og:published_time).{0,80}', html, re.IGNORECASE | re.DOTALL):
        print("  ", repr(m.group(0).replace("\n", " ")[:140]))

    # 4) Transmission_Id / DateId (seen in the fetched page)
    print("\n--- Transmission_Id / DateId (tracking params) ---")
    for m in re.finditer(r'DateId=(\d{8})|Transmission_Id=(\d{12})', html):
        print("  ", m.group(0))

    # 5) Human-style date in body: "Feb 24, 2025" or "Feb. 24, 2025"
    print("\n--- Human date patterns in first 20k chars ---")
    sample = html[:20000]
    for pat in [
        r"[A-Za-z]{3}\.?\s+\d{1,2},?\s+\d{4}(?:\s*,?\s*\d{1,2}:\d{2})?",
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",
    ]:
        for m in re.finditer(pat, sample):
            print("  ", repr(m.group(0)))

if __name__ == "__main__":
    main()
