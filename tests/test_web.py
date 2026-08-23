from autogatus.health import ContainerHealth, Verdict
from autogatus.web import _badge, _bytes, _status, _uptime


def test_bytes_formatting():
    assert _bytes(None) == "n/a"
    assert _bytes(512) == "512 B"
    assert _bytes(1024 * 1024 * 1.5) == "1.5 MB"


def test_uptime_formatting():
    assert _uptime(None) == "n/a"
    assert _uptime(90) == "1m"
    assert _uptime(3700) == "1h 1m"
    assert _uptime(90000) == "1d 1h"


def test_status_derivation():
    h = ContainerHealth(name="x", stack="s", state="running")
    assert _status(Verdict(True, "", None), h) == "healthy"
    assert _status(Verdict(False, "", None), h) == "unhealthy"
    stopped = ContainerHealth(name="x", stack="s", state="exited")
    assert _status(Verdict(True, "", None), stopped) == "unhealthy"


def test_badge_html_has_label():
    assert "Healthy" in _badge("healthy")
    assert "bg-green-500" in _badge("healthy")
    assert "Unhealthy" in _badge("unhealthy")
