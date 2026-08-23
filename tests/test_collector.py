"""collector: uptime parsing edge cases (pure, Docker-free)."""

from autogatus.collector import _uptime_seconds


def test_uptime_zero_date_is_none():
    assert _uptime_seconds("0001-01-01T00:00:00Z") is None


def test_uptime_empty_is_none():
    assert _uptime_seconds("") is None


def test_uptime_garbage_is_none():
    assert _uptime_seconds("not-a-timestamp") is None


def test_uptime_past_timestamp_is_positive():
    assert _uptime_seconds("2020-01-01T00:00:00.000000Z") > 0
