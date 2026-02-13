import unittest

from app.collectors.asset import (
    _extract_landing_locations_html,
    _is_landing_image_collector,
    normalize_properties,
)


class TestLandingAssetFlow(unittest.TestCase):
    def test_is_landing_image_collector_excludes_nav_card(self) -> None:
        """View all homes in City, ST is the per-section image collector — not a property."""
        self.assertTrue(_is_landing_image_collector("View all homes in Albany, NY"))
        self.assertTrue(_is_landing_image_collector("view all homes in Atlanta, GA"))
        self.assertFalse(_is_landing_image_collector("Park South"))
        self.assertFalse(_is_landing_image_collector("Livano Oakwood"))

    def test_extract_landing_locations_html_counts_per_section(self) -> None:
        """Properties are grouped by location (h2); count restarts when new location detected."""
        html = """
        <html><body>
        <h2>Albany, NY</h2>
        <h3>Park South</h3>
        <a href="/s/albany-ny/apartments/furnished">View all homes in Albany, NY</a>
        <h2>Asheville, NC</h2>
        <p>No properties available</p>
        <a href="/s/ashville-nc/apartments/furnished">View all homes in Asheville, NC</a>
        <h2>Atlanta, GA</h2>
        <h3>Livano Oakwood</h3>
        <h3>Cortland Oleander East</h3>
        <h3>Cortland 3131</h3>
        <a href="/s/atlanta-ga/apartments/furnished">View all homes in Atlanta, GA</a>
        </body></html>
        """
        props, loc_counts = _extract_landing_locations_html(
            html,
            "https://www.hellolanding.com/locations",
            "https://www.hellolanding.com",
        )
        # Albany: 1, Asheville: 0, Atlanta: 3.
        self.assertEqual(len(props), 4)
        names = {p["name"] for p in props}
        self.assertIn("Park South", names)
        self.assertIn("Livano Oakwood", names)
        self.assertNotIn("View all homes in Albany, NY", names)
        self.assertEqual(loc_counts[0]["market"], "Albany, NY")
        self.assertEqual(loc_counts[0]["count"], 1)
        self.assertEqual(loc_counts[1]["market"], "Asheville, NC")
        self.assertEqual(loc_counts[1]["count"], 0)
        self.assertEqual(loc_counts[2]["market"], "Atlanta, GA")
        self.assertEqual(loc_counts[2]["count"], 3)

    def test_extract_landing_locations_includes_zero_property_markets(self) -> None:
        """Markets with no properties should appear in location_counts (upcoming areas)."""
        html = """
        <html><body>
        <h2>Corpus Christi, TX</h2>
        <p>No properties available</p>
        <a href="/s/corpus-christi-tx/apartments/furnished">View all homes in Corpus Christi, TX</a>
        </body></html>
        """
        props, loc_counts = _extract_landing_locations_html(
            html,
            "https://www.hellolanding.com/locations",
            "https://www.hellolanding.com",
        )
        self.assertEqual(len(props), 0)
        self.assertEqual(len(loc_counts), 1)
        self.assertEqual(loc_counts[0]["market"], "Corpus Christi, TX")
        self.assertEqual(loc_counts[0]["count"], 0)

    def test_normalize_properties_excludes_view_all_homes(self) -> None:
        """Normalize should drop 'View all homes in X' as junk name."""
        props = [
            {"url": "https://example.com/park-south", "name": "Park South", "market": "Albany, NY"},
            {"url": "https://example.com/s/albany/apartments", "name": "View all homes in Albany, NY", "market": "Albany, NY"},
        ]
        out = normalize_properties(props)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "Park South")

    def test_normalize_properties_does_not_dedupe_same_name_different_city(self) -> None:
        """Same property name in different cities/markets must remain separate (cross-city listings)."""
        props = [
            {"url": None, "name": "The 55 Elm Club", "market": "Hartford, CT", "city": None, "state": None},
            {"url": None, "name": "The 55 Elm Club", "market": "New Haven, CT", "city": None, "state": None},
            {"url": "https://example.com/legacy-fort-clarke", "name": "Legacy at Fort Clarke", "market": "Gainesville, FL", "city": None, "state": None},
            {"url": "https://example.com/legacy-fort-clarke", "name": "Legacy at Fort Clarke", "market": "Ocala, FL", "city": None, "state": None},
        ]
        out = normalize_properties(props)
        self.assertEqual(len(out), 4, "Same name/URL in different cities should not be combined")
        markets = [p["market"] for p in out]
        self.assertIn("Hartford, CT", markets)
        self.assertIn("New Haven, CT", markets)
        self.assertIn("Gainesville, FL", markets)
        self.assertIn("Ocala, FL", markets)


if __name__ == "__main__":
    unittest.main()
