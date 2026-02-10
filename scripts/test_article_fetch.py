#!/usr/bin/env python3
"""
Test fetching full article text for a few real URLs (same as press summarizer).
Run from repo root: python scripts/test_article_fetch.py

Findings from a quick run:
- PR Newswire: 200 OK, full HTML. Extracting from <article> first gives the release body;
  full-body extraction used to send nav/chrome first (fixed in app via _html_to_article_text).
- Some outlets (e.g. Reuters): 401 or "Please enable JS" — we get little text; summarizer
  falls back to title/outlet when extracted text < 200 chars.
- Paywalls / consent walls: same — little extractable text, we fall back to title-only.
"""
import re
import sys
from pathlib import Path

# Allow importing app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors.http import fetch_url


def html_to_text(html: str, max_chars: int = 6000) -> str:
    """Same logic as llm_structured._html_to_text_for_enricher (no location attrs)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html[:max_chars] + "\n[... truncated]" if len(html) > max_chars else html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    body = soup.find("body") or soup
    text = body.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[... truncated]"
    return text


# Sample URLs that might appear in our pipeline (PR Newswire, BI, CNBC, company press)
TEST_URLS = [
    "https://www.prnewswire.com/news-releases/avantstay-and-wander-announce-strategic-partnership-to-expand-luxury-distribution-and-supply-growth-302662143.html",
    "https://www.cnbc.com/2024/01/10/sample-article.html",  # may 404; we'll see
    "https://www.businessinsider.com/",
    "https://news.google.com/",  # Google News redirects; article links are often to publishers
]

# Real article URLs (public, no paywall) to test
REAL_TEST_URLS = [
    "https://www.prnewswire.com/news-releases/avantstay-and-wander-announce-strategic-partnership-to-expand-luxury-distribution-and-supply-growth-302662143.html",
    "https://www.prnewswire.com/news-releases/avantstay-expands-hotel-portfolio-with-grand-opening-in-miami-302656026.html",
    "https://www.reuters.com/business/",  # section page, not article
]

def main():
    print("Fetching articles (User-Agent: competitor-signals/0.1, timeout=20)...\n")
    for url in REAL_TEST_URLS:
        print(f"URL: {url[:80]}...")
        try:
            r = fetch_url(url, timeout=20)
            print(f"  Status: {r.status_code}, Content-Type: {r.content_type or 'N/A'}")
            print(f"  Raw HTML length: {len(r.text)} chars")
            text = html_to_text(r.text, max_chars=6000)
            print(f"  Extracted text length: {len(text)} chars")
            if len(text) < 200:
                print(f"  WARNING: Very little extractable text (paywall/JS/consent wall?)")
                # Show first 300 chars of text to see what we got
                print(f"  Preview: {repr(text[:300])}")
            else:
                preview = text[:200].replace("\n", " ")
                print(f"  Preview: {preview}...")
            # Heuristics for common issues
            lower = r.text.lower()
            if "subscribe" in lower and "read" in lower and len(text) < 500:
                print("  LIKELY: Paywall or subscribe-to-read (little text + subscribe CTA)")
            if "cookie" in lower and "consent" in lower and len(text) < 300:
                print("  LIKELY: Consent/cookie wall (content behind accept)")
            if "access denied" in lower or "blocked" in lower or "403" in r.text:
                print("  LIKELY: Blocked (403 / access denied)")
            if "<article" not in lower and "<main" not in lower and len(r.text) > 5000 and len(text) < 400:
                print("  POSSIBLE: Main content in JS (article/main not in HTML, little text)")
            print()
        except Exception as e:
            print(f"  ERROR: {e}\n")

if __name__ == "__main__":
    main()
