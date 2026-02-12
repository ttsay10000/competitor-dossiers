#!/usr/bin/env python3
"""
Save LinkedIn session for authenticated scraping.

Opens a browser; you log into LinkedIn manually. When done, the session
(storage state) is saved to linkedin_state.json. Set:

  export LINKEDIN_STORAGE_STATE_PATH=/path/to/linkedin_state.json

Then run the social channel; it will use this session to fetch company posts.

Usage:
  python3 -m scripts.save_linkedin_session

The script keeps the browser open. Visit linkedin.com, sign in, then press
Enter in the terminal to save and exit.
"""
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUTPUT_PATH = ROOT / "linkedin_state.json"


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Install playwright: pip install playwright && playwright install chromium")
        sys.exit(1)

    print("Opening browser. Log into LinkedIn, then press Enter here to save the session.")
    print(f"Session will be saved to: {OUTPUT_PATH}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = context.new_page()
        page.goto("https://www.linkedin.com/feed/")
        input("Press Enter after you've logged in to save the session...")
        context.storage_state(path=str(OUTPUT_PATH))
        browser.close()

    print(f"Saved. Set in your environment:")
    print(f"  export LINKEDIN_STORAGE_STATE_PATH={OUTPUT_PATH}")
    print()
    print("Then run: python3 -m app.cli --channel social")


if __name__ == "__main__":
    main()
