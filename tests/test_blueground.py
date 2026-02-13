"""Tests for Blueground asset and press flow (parsing, no DB/network)."""
import pytest

from app.collectors.asset import (
    _parse_blueground_slug_city_state,
    normalize_properties,
)


class TestBluegroundSlugParsing:
    """Unit tests for Blueground destination slug parsing."""

    def test_usa_slug_city_state(self):
        city, state = _parse_blueground_slug_city_state("acton-ma-usa")
        assert city == "Acton"
        assert state == "MA"

    def test_multi_word_city(self):
        city, state = _parse_blueground_slug_city_state("agoura-hills-ca-usa")
        assert city == "Agoura Hills"
        assert state == "CA"

    def test_non_usa_returns_empty(self):
        city, state = _parse_blueground_slug_city_state("london-can")
        assert city == ""
        assert state == ""

    def test_empty_or_invalid(self):
        assert _parse_blueground_slug_city_state("") == ("", "")
        assert _parse_blueground_slug_city_state("atlanta-ga") == ("", "")


class TestBluegroundPropertyNormalize:
    """Blueground-style properties normalize correctly."""

    def test_normalize_blueground_property(self):
        raw = [
            {
                "url": "https://www.theblueground.com/p/furnished-apartments/bos-123",
                "name": "#1A • Some Building",
                "market": "Acton, MA",
                "city": "Acton",
                "state": "MA",
            }
        ]
        out = normalize_properties(raw)
        assert len(out) == 1
        assert out[0].get("name")
        assert out[0].get("state") == "MA"
        assert out[0].get("city") == "Acton"
