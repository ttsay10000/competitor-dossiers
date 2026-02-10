import unittest
from unittest.mock import patch

from app.collectors.asset import (
    collect_asset_snapshot,
    extract_links_from_sitemap,
    is_property_like,
)
from app.collectors.http import FetchResult


class TestAvantstayAssetFlow(unittest.TestCase):
    def test_is_property_like_avantstay_numeric_id_path(self) -> None:
        """AvantStay uses /{id}/{destination}/{slug} (e.g. /429468/newport-beach/sand-castle)."""
        self.assertTrue(is_property_like("https://avantstay.com/429468/newport-beach/sand-castle"))
        self.assertTrue(is_property_like("https://avantstay.com/12345/austin-tx/atlas"))
        self.assertFalse(is_property_like("https://avantstay.com/blog/"))
        self.assertFalse(is_property_like("https://avantstay.com/429468"))  # only one segment after id

    def test_extract_links_from_sitemap_parses_valid_xml(self) -> None:
        """Basic sanity check: valid sitemap XML yields loc URLs."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://avantstay.com/property/villa-1</loc></url>
          <url><loc>https://avantstay.com/locations/palm-springs</loc></url>
        </urlset>
        """
        urls = extract_links_from_sitemap(xml)
        self.assertIn("https://avantstay.com/property/villa-1", urls)
        self.assertIn("https://avantstay.com/locations/palm-springs", urls)

    @patch("app.collectors.asset.BeautifulSoup")
    def test_extract_links_from_sitemap_falls_back_when_xml_parser_fails(
        self, mock_bs
    ) -> None:
        """
        If the XML tree builder is unavailable or BeautifulSoup raises,
        extract_links_from_sitemap should return an empty list instead of crashing.
        """

        # Make BeautifulSoup raise for both xml and html.parser invocations.
        mock_bs.side_effect = Exception("no parser available")

        urls = extract_links_from_sitemap("<urlset></urlset>")
        self.assertEqual(urls, [])

    @patch("app.collectors.asset._extract_properties_via_llm", return_value=[])
    @patch("app.collectors.asset.fetch_url")
    @patch("app.collectors.asset.expand_sitemap")
    @patch("app.collectors.asset.discover_sitemap")
    def test_collect_asset_snapshot_sitemap_first_falls_back_to_html(
        self,
        mock_discover_sitemap,
        mock_expand_sitemap,
        mock_fetch_url,
        mock_extract_llm,
    ) -> None:
        """
        For an AvantStay-like config (sitemap_first + js_required, llm_extract=True),
        when the sitemap yields no property-like URLs, the collector should fall back
        to fetching the HTML search page and extracting properties from links.
        """

        # Simulate discover_sitemap returning a sitemap URL that has no property-like URLs.
        mock_discover_sitemap.return_value = ["https://avantstay.com/sitemap.xml"]
        mock_expand_sitemap.return_value = []  # no property URLs from sitemap

        # Fake HTML for https://avantstay.com/search with one property-like link.
        html = """
        <html>
          <body>
            <a href="/property/villa-1">Villa 1</a>
          </body>
        </html>
        """
        mock_fetch_url.return_value = FetchResult(
            url="https://avantstay.com/search",
            status_code=200,
            content_type="text/html",
            text=html,
            raw_hash="dummy-hash",
        )

        snapshot = collect_asset_snapshot(
            "https://avantstay.com/search",
            js_required=True,
            use_sitemap_first=True,
            extra_options={"llm_extract": True},
        )

        properties = snapshot.get("properties") or []
        self.assertTrue(properties, "Expected at least one property from HTML fallback")
        names = {p.get("name") for p in properties}
        self.assertIn("Villa 1", names)
        # When llm_extract=True but LLM returns nothing, we should mark note="html".
        self.assertEqual(snapshot.get("note"), "html")

    @patch("app.collectors.asset.fetch_url")
    def test_collect_asset_snapshot_raises_on_non_200_html(self, mock_fetch_url) -> None:
        """
        When the HTML fetch for the source URL returns a non-200 status,
        collect_asset_snapshot should raise a RuntimeError so Runs can log an error.
        """
        mock_fetch_url.return_value = FetchResult(
            url="https://avantstay.com/search",
            status_code=500,
            content_type="text/html",
            text="",
            raw_hash="dummy-hash",
        )

        with self.assertRaises(RuntimeError):
            collect_asset_snapshot(
                "https://avantstay.com/search",
                js_required=False,
                use_sitemap_first=False,
                extra_options={},
            )


if __name__ == "__main__":
    unittest.main()

