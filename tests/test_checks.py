from unittest.mock import MagicMock

from autogatus.checks import (
    Check,
    _parse_tcp,
    evaluate_exec,
    evaluate_tcp,
    parse_container_checks,
    parse_duration_seconds,
    run_check,
)

# ── label parsing ─────────────────────────────────────────────────────────────


def test_no_check_labels_yields_nothing():
    checks, disabled = parse_container_checks({}, "svc", "mystack")
    assert checks == []
    assert disabled is False


def test_tcp_check_defaults():
    labels = {"autogatus.check.web.tcp": "80"}
    checks, _ = parse_container_checks(labels, "whoami", "web-stack")
    assert len(checks) == 1
    c = checks[0]
    assert c.kind == "tcp"
    assert c.tcp_host == "whoami"  # host defaults to container name
    assert c.tcp_port == 80
    assert c.name == "whoami-web"  # default <container>-<id>
    assert c.group == "web-stack"  # default = stack


def test_tcp_host_port_form():
    labels = {"autogatus.check.db.tcp": "postgres:5432"}
    checks, _ = parse_container_checks(labels, "app", "s")
    assert checks[0].tcp_host == "postgres"
    assert checks[0].tcp_port == 5432


def test_tcp_malformed_skipped():
    labels = {"autogatus.check.x.tcp": "notaport"}
    checks, _ = parse_container_checks(labels, "app", "s")
    assert checks == []


def test_exec_gated_off_by_default():
    labels = {"autogatus.check.alive.exec": "true"}
    checks, disabled = parse_container_checks(labels, "app", "s", exec_enabled=False)
    assert checks == []
    assert disabled is True


def test_exec_enabled():
    labels = {"autogatus.check.alive.exec": "pgrep nginx"}
    checks, disabled = parse_container_checks(labels, "app", "s", exec_enabled=True)
    assert disabled is False
    assert len(checks) == 1
    assert checks[0].kind == "exec"
    assert checks[0].target == "pgrep nginx"


def test_overrides_name_group_interval_timeout_alerts():
    labels = {
        "autogatus.check.c.tcp": "80",
        "autogatus.check.c.name": "front door",
        "autogatus.check.c.group": "edge",
        "autogatus.check.c.interval": "30s",
        "autogatus.check.c.timeout": "2.5",
        "autogatus.check.c.alerts": "custom,ntfy",
    }
    c = parse_container_checks(labels, "app", "s")[0][0]
    assert c.name == "front door"
    assert c.group == "edge"
    assert c.interval == "30s"
    assert c.timeout == 2.5
    assert [a["type"] for a in c.alerts] == ["custom", "ntfy"]


def test_alerts_default_applied_when_absent():
    # A cascaded check inherits the provider but stamps its own description
    # (here the fallback, since no alert-description label is set).
    labels = {"autogatus.check.c.tcp": "80"}
    default = [{"type": "custom", "description": "d"}]
    c = parse_container_checks(labels, "app", "s", default_alerts=default)[0][0]
    assert c.alerts == [{"type": "custom", "description": "app-c check"}]


def test_multiple_checks_per_container_distinct_ids():
    labels = {
        "autogatus.check.web.tcp": "80",
        "autogatus.check.metrics.tcp": "9090",
    }
    checks, _ = parse_container_checks(labels, "app", "s")
    names = sorted(c.name for c in checks)
    assert names == ["app-metrics", "app-web"]  # no collision


def test_check_missing_target_skipped():
    labels = {"autogatus.check.c.name": "orphan"}
    checks, _ = parse_container_checks(labels, "app", "s")
    assert checks == []


def test_parse_tcp_helper():
    assert _parse_tcp("80", "c") == ("c", 80)
    assert _parse_tcp("h:81", "c") == ("h", 81)
    assert _parse_tcp("bad", "c") is None
    assert _parse_tcp("", "c") is None


# ── execution / verdicts ──────────────────────────────────────────────────────


def test_evaluate_exec_success():
    res = MagicMock(exit_code=0, output=b"ok\n")
    container = MagicMock()
    container.exec_run.return_value = res
    ok, detail = evaluate_exec(container, "true", 5)
    assert ok is True
    assert "exit=0" in detail
    assert "out=ok" in detail


def test_evaluate_exec_failure():
    res = MagicMock(exit_code=1, output=b"boom")
    container = MagicMock()
    container.exec_run.return_value = res
    ok, detail = evaluate_exec(container, "false", 5)
    assert ok is False
    assert "exit=1" in detail
    assert "boom" in detail


def test_evaluate_tcp_failure_on_closed_port():
    # 127.0.0.1:1 is virtually always closed
    ok, detail = evaluate_tcp("127.0.0.1", 1, 1)
    assert ok is False
    assert "tcp 127.0.0.1:1" in detail


def test_run_check_wraps_verdict():
    chk = Check(
        id="c",
        kind="tcp",
        target="127.0.0.1:1",
        name="n",
        group="g",
        interval="90s",
        description="d",
        tcp_host="127.0.0.1",
        tcp_port=1,
    )
    v = run_check(chk, MagicMock())
    assert v.success is False
    assert v.error.startswith("check failed: tcp")
    assert v.headline is None


def test_run_check_exec_success_verdict():
    res = MagicMock(exit_code=0, output=b"")
    container = MagicMock()
    container.exec_run.return_value = res
    chk = Check(
        id="c", kind="exec", target="true", name="n", group="g", interval="90s", description="d"
    )
    v = run_check(chk, container)
    assert v.success is True
    assert v.error.startswith("exec ")


# ── pre-1.0 naming and behavior ───────────────────────────────────────────────


def test_alert_description_alias_preferred():
    labels = {"autogatus.check.x.tcp": "80", "autogatus.check.x.alert-description": "db down"}
    c = parse_container_checks(labels, "app", "s")[0][0]
    assert c.description == "db down"


def test_description_legacy_alias_still_works():
    labels = {"autogatus.check.x.tcp": "80", "autogatus.check.x.description": "old style"}
    c = parse_container_checks(labels, "app", "s")[0][0]
    assert c.description == "old style"


def test_check_inherits_default_alerts():
    # cascade at the parse level: a check with no .alerts inherits the provider,
    # but the description names the check, not the container.
    labels = {"autogatus.check.x.tcp": "80"}
    default = [{"type": "ntfy", "description": "cascaded"}]
    c = parse_container_checks(labels, "app", "s", default_alerts=default)[0][0]
    assert c.alerts == [{"type": "ntfy", "description": "app-x check"}]
    # the passed-in default is not mutated (no shared aliasing)
    assert default == [{"type": "ntfy", "description": "cascaded"}]


def test_check_own_alerts_override_default():
    labels = {"autogatus.check.x.tcp": "80", "autogatus.check.x.alerts": "custom"}
    default = [{"type": "ntfy", "description": "cascaded"}]
    c = parse_container_checks(labels, "app", "s", default_alerts=default)[0][0]
    assert [a["type"] for a in c.alerts] == ["custom"]


def test_check_none_does_not_cascade():
    labels = {"autogatus.check.x.tcp": "80", "autogatus.check.x.alerts": "none"}
    default = [{"type": "ntfy", "description": "cascaded"}]
    c = parse_container_checks(labels, "app", "s", default_alerts=default)[0][0]
    assert c.alerts == []


def test_check_structured_alerts():
    labels = {
        "autogatus.check.x.tcp": "80",
        "autogatus.check.x.alerts.0.type": "custom",
        "autogatus.check.x.alerts.0.failure-threshold": "3",
    }
    c = parse_container_checks(labels, "app", "s")[0][0]
    assert c.alerts == [{"type": "custom", "failure-threshold": 3, "description": "app-x check"}]


def test_check_run_interval_default():
    labels = {"autogatus.check.x.tcp": "80"}
    c = parse_container_checks(labels, "app", "s", default_interval="20s")[0][0]
    assert c.interval == "20s"


def test_parse_duration_seconds():
    assert parse_duration_seconds("30s", 0) == 30
    assert parse_duration_seconds("5m", 0) == 300
    assert parse_duration_seconds("2h", 0) == 7200
    assert parse_duration_seconds("100ms", 0) == 0.1
    assert parse_duration_seconds("15", 0) == 15  # bare number is seconds
    assert parse_duration_seconds("", 9) == 9  # empty falls back
    assert parse_duration_seconds("bogus", 9) == 9  # unparseable falls back
    assert parse_duration_seconds(None, 9) == 9
