"""Tests for runner channel dispatch and seed baseline."""

from app.runner import run, RUNNER_CHANNELS


def test_run_unknown_channel_prints_message(capsys):
    run(channel="invalid_channel")
    out = capsys.readouterr().out
    assert "unknown channel" in out
    assert "invalid_channel" in out


def test_runner_channels_include_all_seed_baseline_channels():
    """Every channel in RUNNER_CHANNELS has per-competitor seed baseline (SEED_MODE).
    Do not remove channels from this list without updating the runner seed logic."""
    expected = ("talent", "asset", "press", "homepage", "public_records")
    assert RUNNER_CHANNELS == expected
    assert len(RUNNER_CHANNELS) == 5
