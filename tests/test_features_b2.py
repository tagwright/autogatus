"""Tests for the b2 feature set: per-container thresholds, failure latching,
snooze, and the description-label deprecation warning."""

from __future__ import annotations

import datetime
import logging

from autogatus import checks
from autogatus.health import ContainerHealth, Thresholds, Verdict
from autogatus.monitor import ContainerMonitor


class FakeContainer:
    def __init__(self, name="c", labels=None, attrs=None):
        self.name = name
        self.labels = labels or {}
        self.attrs = attrs or {}


def _monitor(thresholds=None, failure_latch=3):
    return ContainerMonitor(
        client=None,
        pusher=None,
        token="t",
        stack_map={},
        thresholds=thresholds if thresholds is not None else Thresholds(),
        excludes=[],
        failure_latch=failure_latch,
    )


def _started(seconds_ago: float) -> str:
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# --- per-container threshold overrides ---


def test_threshold_label_beats_global():
    m = _monitor(Thresholds(mem_percent=95.0, cpu_percent=None))
    t = m._thresholds_for(FakeContainer(labels={"autogatus.mem-threshold": "50"}))
    assert t.mem_percent == 50.0
    assert t.cpu_percent is None


def test_threshold_label_none_disables():
    m = _monitor(Thresholds(mem_percent=95.0))
    t = m._thresholds_for(FakeContainer(labels={"autogatus.mem-threshold": "none"}))
    assert t.mem_percent is None


def test_threshold_no_label_returns_global_unchanged():
    m = _monitor(Thresholds(mem_percent=95.0))
    assert m._thresholds_for(FakeContainer(labels={})) is m.thresholds


def test_cpu_threshold_label():
    m = _monitor(Thresholds(mem_percent=95.0, cpu_percent=None))
    t = m._thresholds_for(FakeContainer(labels={"autogatus.cpu-threshold": "300"}))
    assert t.cpu_percent == 300.0


# --- failure latching ---


def _running(restart_count=0, oom=False):
    return ContainerHealth(
        name="x", stack="s", state="running", restart_count=restart_count, oom_killed=oom
    )


def test_oom_latches_for_configured_cycles():
    m = _monitor(failure_latch=3)
    # Event cycle: OOM, verdict already failing from evaluate.
    v0 = m._apply_latch(
        "x", _running(oom=True), prev_restart=0, verdict=Verdict(False, "OOMKilled", None)
    )
    assert not v0.success
    # Next cycles recover in evaluate but the latch holds them failing.
    v1 = m._apply_latch("x", _running(), prev_restart=0, verdict=Verdict(True, "ok", None))
    v2 = m._apply_latch("x", _running(), prev_restart=0, verdict=Verdict(True, "ok", None))
    assert not v1.success and not v2.success
    assert "latched" in v1.error
    # Latch expired: back to the real verdict.
    v3 = m._apply_latch("x", _running(), prev_restart=0, verdict=Verdict(True, "ok", None))
    assert v3.success


def test_restart_bump_latches():
    m = _monitor(failure_latch=2)
    v0 = m._apply_latch(
        "x", _running(restart_count=3), prev_restart=2, verdict=Verdict(True, "ok", None)
    )
    assert not v0.success  # restart bump detected, forced failing
    v1 = m._apply_latch(
        "x", _running(restart_count=3), prev_restart=3, verdict=Verdict(True, "ok", None)
    )
    assert not v1.success
    v2 = m._apply_latch(
        "x", _running(restart_count=3), prev_restart=3, verdict=Verdict(True, "ok", None)
    )
    assert v2.success


def test_latch_disabled_when_zero():
    m = _monitor(failure_latch=0)
    v = m._apply_latch("x", _running(oom=True), prev_restart=0, verdict=Verdict(True, "ok", None))
    assert v.success  # no latching, and evaluate's own verdict passes through


# --- snooze ---


def test_snoozed_while_young():
    m = _monitor()
    c = FakeContainer(
        labels={"autogatus.snooze": "1m"}, attrs={"State": {"StartedAt": _started(10)}}
    )
    assert m._snoozed(c) is True


def test_not_snoozed_after_window():
    m = _monitor()
    c = FakeContainer(
        labels={"autogatus.snooze": "1m"}, attrs={"State": {"StartedAt": _started(600)}}
    )
    assert m._snoozed(c) is False


def test_no_snooze_label():
    m = _monitor()
    c = FakeContainer(labels={}, attrs={"State": {"StartedAt": _started(1)}})
    assert m._snoozed(c) is False


# --- description label deprecation ---


def test_description_alias_warns_once(caplog):
    checks._warned_desc_alias.clear()
    labels = {"autogatus.check.a.tcp": "80", "autogatus.check.a.description": "old"}
    with caplog.at_level(logging.WARNING):
        checks.parse_container_checks(labels, "cont", "stack")
        checks.parse_container_checks(labels, "cont", "stack")
    warns = [
        r
        for r in caplog.records
        if "deprecated" in r.getMessage() and "description" in r.getMessage()
    ]
    assert len(warns) == 1


def test_alert_description_does_not_warn(caplog):
    checks._warned_desc_alias.clear()
    labels = {"autogatus.check.a.tcp": "80", "autogatus.check.a.alert-description": "new"}
    with caplog.at_level(logging.WARNING):
        checks.parse_container_checks(labels, "cont", "stack")
    assert not [r for r in caplog.records if "deprecated" in r.getMessage()]
