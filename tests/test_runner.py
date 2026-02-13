"""Tests for runner channel dispatch and seed baseline."""

from app.runner import run, RUNNER_CHANNELS


def test_run_unknown_channel_prints_message(capsys):
    run(channel="invalid_channel")
    out = capsys.readouterr().out
    assert "unknown channel" in out
    assert "invalid_channel" in out


def test_runner_channels_include_homepage():
    """Homepage channel is required for website changes / digital footprint flow."""
    assert "homepage" in RUNNER_CHANNELS
    assert RUNNER_CHANNELS == (
        "talent", "asset", "press", "homepage", "public_records", "reviews", "social"
    )
