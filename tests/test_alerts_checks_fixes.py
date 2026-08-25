"""b2 alerts and checks correctness gates: reminder-interval duration, per-check
descriptions, deduped and cached allowlist warnings, wedged-check skipping, and
key-collision detection."""

import threading

from autogatus import __main__ as agmain
from autogatus import alerts as alerts_mod
from autogatus.alerts import filter_alerts, parse_alerts
from autogatus.checks import parse_container_checks
from autogatus.health import Thresholds
from autogatus.monitor import ContainerMonitor, _gatus_key


class FakeContainer:
    def __init__(self, name, status="running", labels=None):
        self.name = name
        self.status = status
        self.labels = labels or {}
        self.attrs = {
            "State": {
                "Status": status,
                "ExitCode": 0,
                "OOMKilled": False,
                "StartedAt": "0001-01-01T00:00:00Z",
            },
            "RestartCount": 0,
        }

    def stats(self, stream=False):
        return {}


class FakeClient:
    def __init__(self, containers):
        self._containers = containers

    class _C:
        def __init__(self, outer):
            self._outer = outer

        def list(self, all=False, ignore_removed=False):
            return self._outer._containers

    @property
    def containers(self):
        return FakeClient._C(self)


def _monitor(client, **kw):
    return ContainerMonitor(
        client=client,
        pusher=None,
        token="t",
        stack_map={},
        thresholds=Thresholds(),
        excludes=[],
        **kw,
    )


# --- Fix 1: minimum-reminder-interval is a duration string, not an int ---


def test_minimum_reminder_interval_stays_a_string():
    fields = {"alerts.0.type": "ntfy", "alerts.0.minimum-reminder-interval": "10m"}
    alerts = parse_alerts(fields, default_description="d")
    assert alerts[0]["minimum-reminder-interval"] == "10m"
    assert not isinstance(alerts[0]["minimum-reminder-interval"], int)


# --- Fix 2: a cascaded check keeps its own description, no aliasing ---


def test_check_inherits_alerts_but_keeps_own_description():
    container_alerts = [
        {"type": "ntfy", "failure-threshold": 5, "description": "container is down"}
    ]
    labels = {
        "autogatus.check.ddns.exec": "pgrep ddns",
        "autogatus.check.ddns.alert-description": "ddns updater died",
    }
    checks, _ = parse_container_checks(
        labels, "cf", "net", default_alerts=container_alerts, exec_enabled=True
    )
    a = checks[0].alerts
    assert a[0]["type"] == "ntfy"
    assert a[0]["failure-threshold"] == 5  # inherited provider and threshold
    assert a[0]["description"] == "ddns updater died"  # the check's own, not the container's
    # The container's declaration must be untouched (no shared-dict aliasing).
    assert container_alerts[0]["description"] == "container is down"


# --- Fix 3a: the unconfigured-provider warning is deduped ---


def test_drop_warning_fires_once_not_every_cycle(caplog):
    alerts_mod._warned_drops.clear()
    a = [{"type": "ntfy", "description": "d"}]
    with caplog.at_level("WARNING"):
        filter_alerts(a, {"custom"}, "grp/x")
        filter_alerts(a, {"custom"}, "grp/x")
    warns = [r for r in caplog.records if "dropping unconfigured" in r.getMessage()]
    assert len(warns) == 1


def test_drop_warning_rewarns_after_recovery(caplog):
    alerts_mod._warned_drops.clear()
    a = [{"type": "ntfy", "description": "d"}]
    with caplog.at_level("WARNING"):
        filter_alerts(a, {"custom"}, "k")  # dropped, warns
        filter_alerts(a, {"ntfy"}, "k")  # kept, clears the flag
        filter_alerts(a, {"custom"}, "k")  # dropped again, warns again
    warns = [r for r in caplog.records if "dropping unconfigured" in r.getMessage()]
    assert len(warns) == 2


# --- Fix 3b: a transient config-read failure reuses the cached allowlist ---


def _reset_allowlist_cache(monkeypatch, path, env_types):
    monkeypatch.setattr(agmain, "GATUS_CONFIG_PATH", path)
    monkeypatch.setattr(agmain, "ALERT_TYPES", env_types)
    monkeypatch.setattr(agmain, "_cached_allowlist", None)
    monkeypatch.setattr(agmain, "_warned_stale_config", False)


def test_allowlist_reuses_last_good_read_on_failure(tmp_path, monkeypatch):
    cfg = tmp_path / "gatus.yaml"
    cfg.write_text("alerting:\n  custom: {}\n  ntfy: {}\n")
    _reset_allowlist_cache(monkeypatch, str(cfg), [])

    allow, _ = agmain._resolve_allowlist()
    assert allow == {"custom", "ntfy"}

    cfg.write_text("")  # now transiently empty/unreadable
    allow2, src2 = agmain._resolve_allowlist()
    assert allow2 == {"custom", "ntfy"}  # reused cache, not stripped to the fallback
    assert "cached" in src2


def test_allowlist_falls_back_when_never_read(tmp_path, monkeypatch):
    cfg = tmp_path / "gatus.yaml"
    cfg.write_text("")  # empty from the start, never a good read
    _reset_allowlist_cache(monkeypatch, str(cfg), [])
    allow, _ = agmain._resolve_allowlist()
    assert allow == {"custom"}


# --- Fix 4: a wedged check is not relaunched ---


def test_wedged_check_is_not_relaunched(caplog):
    c = FakeContainer("svc", labels={"autogatus.check.web.tcp": "127.0.0.1:1"})
    m = _monitor(FakeClient([c]), resync_interval=1)
    m.reconcile()  # first cycle runs the check
    key = _gatus_key(m._stack_for(c), "svc-web")
    assert key in m._check_last_run

    # Simulate the previous run still wedged, and force the check due again.
    ev = threading.Event()
    t = threading.Thread(target=ev.wait, daemon=True)
    t.start()
    m._check_inflight[key] = t
    m._check_last_run[key] = 0.0

    with caplog.at_level("WARNING"):
        m.reconcile()
    assert m._check_last_run[key] == 0.0  # skipped, last-run not advanced
    assert any("still running from a previous cycle" in r.getMessage() for r in caplog.records)
    ev.set()


# --- Fix 5: colliding sanitized keys are detected ---


def test_key_collision_warns(caplog):
    c1 = FakeContainer("foo_bar")
    c2 = FakeContainer("foo-bar")
    m = _monitor(FakeClient([c1, c2]))
    with caplog.at_level("WARNING"):
        m.reconcile()
    assert any("key collision" in r.getMessage() for r in caplog.records)
