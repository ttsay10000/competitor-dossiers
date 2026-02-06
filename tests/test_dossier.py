"""Tests for dossier/summary and recommendations."""

from app.routes.dossier import build_recommendations


class _Event:
    def __init__(self, type_: str, title: str = ""):
        self.type = type_
        self.title = title or type_


def test_build_recommendations_empty():
    recs = build_recommendations([])
    assert len(recs) == 1
    assert "monitoring" in recs[0]["action"].lower() or "no specific" in recs[0]["action"].lower()


def test_build_recommendations_dedupes_by_type():
    events = [
        _Event("asset.new_market", "New market: Austin"),
        _Event("asset.new_market", "New market: Denver"),
    ]
    recs = build_recommendations(events)
    assert len(recs) == 1
    assert recs[0]["event_type"] == "asset.new_market"


def test_build_recommendations_maps_known_types():
    events = [_Event("talent.hiring_surge"), _Event("capital.fundraise_or_restructuring")]
    recs = build_recommendations(events)
    assert len(recs) == 2
    types = {r["event_type"] for r in recs}
    assert "talent.hiring_surge" in types
    assert "capital.fundraise_or_restructuring" in types
