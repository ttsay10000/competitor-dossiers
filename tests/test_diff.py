from app.diff.talent_diff import diff_jobs
from app.diff.asset_diff import diff_properties
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
