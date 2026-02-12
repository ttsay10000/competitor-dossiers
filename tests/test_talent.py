"""Tests for talent collector (Kula/AvantStay location cleaning and extraction)."""
import pytest

from app.collectors.talent import (
    _clean_location_field,
    extract_jobs_from_kula,
    normalize_jobs,
)


class TestCleanLocationField:
    """Location field cleaning strips salary/USD artifacts (css-7pftiu can contain both)."""

    def test_strips_trailing_equals_dollar(self):
        assert _clean_location_field("Poconos == $0") == "Poconos"
        assert _clean_location_field("Blakeslee, Pennsylvania, United States == $0") == "Blakeslee, Pennsylvania, United States"

    def test_strips_trailing_usd_amount(self):
        assert _clean_location_field("Austin, TX  $50,000") == "Austin, TX"
        assert _clean_location_field("Remote  USD $75,000 - $90,000") == "Remote"

    def test_strips_leading_salary_if_concatenated(self):
        assert _clean_location_field("$0 Blakeslee, Pennsylvania, United States") == "Blakeslee, Pennsylvania, United States"

    def test_pure_location_unchanged(self):
        assert _clean_location_field("Blakeslee, Pennsylvania, United States") == "Blakeslee, Pennsylvania, United States"
        assert _clean_location_field("Remote") == "Remote"

    def test_none_and_empty(self):
        assert _clean_location_field(None) is None
        assert _clean_location_field("") is None
        assert _clean_location_field("   ") is None
        assert _clean_location_field(" == $0") is None


class TestNormalizeJobsCleansLocation:
    """normalize_jobs applies location cleaning to every job."""

    def test_normalize_strips_salary_from_location(self):
        jobs = [{"job_id": None, "title": "Manager", "location": "Poconos == $0", "dept": None, "posted_date": None, "url": None}]
        out = normalize_jobs(jobs)
        assert out[0]["location"] == "Poconos"


class TestExtractJobsFromKula:
    """Kula/AvantStay extraction uses f8zk62 job lines and cleans location."""

    def test_extracts_job_line_and_cleans_location(self):
        html = """
        <p class="chakra-text css-f8zk62">Assistant Area Manager, Poconos == $0</p>
        """
        jobs = extract_jobs_from_kula(html)
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Assistant Area Manager"
        assert jobs[0]["location"] == "Poconos"

    def test_ignores_7pftiu_only_paragraphs(self):
        # Only f8zk62 is used for job lines; 7pftiu-only should not create a job row
        html = """
        <p class="chakra-text css-7pftiu">Blakeslee, Pennsylvania, United States</p>
        """
        jobs = extract_jobs_from_kula(html)
        assert len(jobs) == 0
