from autogatus.monitor import ContainerMonitor


class FakeContainer:
    def __init__(self, labels):
        self.labels = labels
        self.name = "c"


def _monitor(default_alert_types):
    return ContainerMonitor(
        client=None,
        pusher=None,
        token="t",
        stack_map={},
        thresholds=None,
        excludes=[],
        default_alert_types=default_alert_types,
    )


def _types(alerts):
    return [a["type"] for a in alerts]


def test_default_alert_types_when_no_label():
    m = _monitor(["custom", "ntfy"])
    assert _types(m._alerts_for(FakeContainer({}))) == ["custom", "ntfy"]


def test_label_overrides_default():
    m = _monitor(["custom"])
    got = m._alerts_for(FakeContainer({"autogatus.alerts": "ntfy,discord"}))
    assert _types(got) == ["ntfy", "discord"]


def test_label_none_disables():
    m = _monitor(["custom"])
    assert m._alerts_for(FakeContainer({"autogatus.alerts": "none"})) == []


def test_default_falls_back_to_custom():
    m = _monitor(None)
    assert _types(m._alerts_for(FakeContainer({}))) == ["custom"]


def test_container_structured_alerts():
    m = _monitor(["custom"])
    got = m._alerts_for(
        FakeContainer(
            {
                "autogatus.alerts.0.type": "ntfy",
                "autogatus.alerts.0.failure-threshold": "5",
                "autogatus.alerts.0.send-on-resolved": "true",
            }
        )
    )
    assert got == [
        {"type": "ntfy", "failure-threshold": 5, "send-on-resolved": True, "description": "c (c)"}
    ]


# ── reconcile-level: opt-out and alert cascade ────────────────────────────────

from autogatus.health import Thresholds  # noqa: E402


class _RunContainer:
    """A container reconcile can fully process (status, attrs, stats, exec)."""

    def __init__(self, name, labels, status="running"):
        self.name = name
        self.labels = labels
        self.status = status
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

    def exec_run(self, *a, **k):
        class _R:
            exit_code = 0
            output = b""

        return _R()


class _RunClient:
    def __init__(self, containers):
        self._containers = containers

    class _C:
        def __init__(self, outer):
            self._outer = outer

        def list(self, all=False, ignore_removed=False):
            return self._outer._containers

    @property
    def containers(self):
        return _RunClient._C(self)


def _run_monitor(containers, default_alert_types=None, exec_enabled=True):
    return ContainerMonitor(
        client=_RunClient(containers),
        pusher=None,
        token="t",
        stack_map={},
        thresholds=Thresholds(),
        excludes=[],
        default_alert_types=default_alert_types,
        exec_enabled=exec_enabled,
    )


def test_enable_false_opts_out_of_liveness():
    c = _RunContainer("c", {"autogatus.enable": "false"})
    decls, verdicts = _run_monitor([c]).reconcile()
    assert decls == []  # no liveness endpoint declared
    assert verdicts == []


def test_enable_false_still_runs_declared_checks():
    c = _RunContainer("c", {"autogatus.enable": "false", "autogatus.check.x.exec": "true"})
    decls, _ = _run_monitor([c]).reconcile()
    # liveness opted out, but the declared check endpoint is still there
    assert [d["name"] for d in decls] == ["c-x"]


def test_container_alerts_cascade_to_checks():
    c = _RunContainer("c", {"autogatus.alerts": "ntfy", "autogatus.check.x.exec": "true"})
    decls, _ = _run_monitor([c]).reconcile()
    check = next(d for d in decls if d["name"] == "c-x")
    assert [a["type"] for a in check["alerts"]] == ["ntfy"]


def test_check_own_alerts_beat_container_cascade():
    c = _RunContainer(
        "c",
        {
            "autogatus.alerts": "ntfy",
            "autogatus.check.x.exec": "true",
            "autogatus.check.x.alerts": "custom",
        },
    )
    decls, _ = _run_monitor([c]).reconcile()
    check = next(d for d in decls if d["name"] == "c-x")
    assert [a["type"] for a in check["alerts"]] == ["custom"]
