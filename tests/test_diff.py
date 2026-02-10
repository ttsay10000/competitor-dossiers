from app.diff.talent_diff import diff_jobs
from app.diff.asset_diff import (
    diff_properties,
    infer_location_for_property,
    resolve_destination_slug_to_state,
)
from app.diff.press_diff import diff_items


def test_diff_jobs():
    prev = [{"job_id": "1", "title": "A"}]
    curr = [{"job_id": "1", "title": "A"}, {"job_id": "2", "title": "B"}]
    diff = diff_jobs(prev, curr)
    assert len(diff["added"]) == 1
    assert len(diff["removed"]) == 0


def test_diff_properties():
    prev = [{"url": "a"}]
    curr = [{"url": "a"}, {"url": "b"}]
    diff = diff_properties(prev, curr)
    assert len(diff["added"]) == 1


def test_diff_items():
    prev = [{"url": "a"}]
    curr = [{"url": "a"}, {"url": "b"}]
    diff = diff_items(prev, curr)
    assert len(diff["added"]) == 1


def test_resolve_destination_slug_to_state():
    assert resolve_destination_slug_to_state("coachella-valley") == "California"
    assert resolve_destination_slug_to_state("palm-springs") == "California"
    assert resolve_destination_slug_to_state("austin-tx") == "Texas"
    assert resolve_destination_slug_to_state("nashville") == "Tennessee"
    assert resolve_destination_slug_to_state("unknown-slug-xyz") is None


def test_infer_location_for_property_avantstay_url():
    # Avantstay-style URL: /{id}/{destination}/{slug} -> resolve destination to state
    prop = {"url": "https://avantstay.com/221284/coachella-valley/firefly"}
    assert infer_location_for_property(prop) == "California"
    prop2 = {"url": "https://avantstay.com/296360/palm-springs/the-wesley-hotel-buyout"}
    assert infer_location_for_property(prop2) == "California"
    prop3 = {"url": "https://avantstay.com/12345/austin-tx/atlas"}
    assert infer_location_for_property(prop3) == "Texas"
    # When state is already set, prefer it
    prop4 = {"url": "https://avantstay.com/221284/coachella-valley/firefly", "state": "Nevada"}
    assert infer_location_for_property(prop4) == "Nevada"
