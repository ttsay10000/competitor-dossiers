"""Tests for runner channel dispatch."""

from app.runner import run


def test_run_unknown_channel_prints_message(capsys):
    run(channel="invalid_channel")
    out = capsys.readouterr().out
    assert "unknown channel" in out
    assert "invalid_channel" in out
