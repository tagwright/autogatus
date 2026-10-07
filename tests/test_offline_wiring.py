# SPDX-License-Identifier: Apache-2.0
"""Level 2: the autogatus.offline label wired through the real cycle.

Every test drives ``__main__._tick`` (the per-cycle entrypoint ``run()`` calls in
its loop) with a real ContainerMonitor, a real Writer on a temp path, a scripted
fake Docker client and a recording pusher (tests/fakes.py). An autogatus restart
is simulated the way ``run()`` starts up, with a new monitor and a new writer.
F1 and F2 set the two fault knobs this feature uses, the container listing and
the push, and assert the failure surfaces.

Every test was red under its named one-line mutation (recorded in the build
report).
"""

from __future__ import annotations

import json
import logging
import pathlib

import pytest
from fakes import (
    FakeContainer,
    Rig,
    RunCheckSpy,
    dump_golden,
    normalize,
    run_no_label_fleet,
    started_now,
)

from autogatus import monitor as monitor_mod
from autogatus.offline import DRIFT_PREFIX, OFFLINE_LABEL, PARKED_TEXT, REPARK_HINT

OFF = {OFFLINE_LABEL: "true"}
MC = "minecraft"
MC_KEY = "minecraft_minecraft"
FAILED_START = 'exec: "/nonexistent": stat /nonexistent: no such file or directory'
GOLDEN = pathlib.Path(__file__).parent / "golden" / "no_label_fleet.json"


def _alerts(name: str) -> list:
    return [{"type": "custom", "description": f"{name} ({name})"}]


def _messages(caplog, level=None) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "autogatus" and (level is None or r.levelno == level)
    ]


@pytest.fixture
def spy(monkeypatch):
    s = RunCheckSpy()
    monkeypatch.setattr(monitor_mod, "run_check", s)
    return s


def test_parked_frozen_on_fresh_monitor(tmp_path):
    rig = Rig(tmp_path, [FakeContainer(MC, status="created", labels=OFF)])
    for _ in range(3):
        cycle = rig.tick()
        decl = cycle.ext(MC)
        assert decl == {
            "name": MC,
            "group": MC,
            "token": "tok",
            "heartbeat": {"interval": "90s"},
            "enabled": False,
        }
        assert "alerts" not in decl
        assert cycle.pushes_for(MC_KEY) == []
        assert cycle.pushes == []
        assert rig.store is not None
        item = rig.store.get(MC_KEY)
        assert item is not None
        assert item["verdict"].success is False
        assert item["verdict"].error == PARKED_TEXT
    assert "enabled: false" in cycle.text


def test_key_stable_across_park_and_restart(tmp_path):
    rig = Rig(tmp_path, [FakeContainer(MC, status="running")])
    unparked = rig.tick().ext(MC)
    assert unparked is not None and "alerts" in unparked

    # The label is added and docker compose up --no-start recreates it parked.
    rig.client.fleet = [FakeContainer(MC, status="created", labels=OFF, cid="mc-2")]
    parked = rig.tick().ext(MC)

    rig.restart()
    after_restart = rig.tick()
    restarted = after_restart.ext(MC)

    expected = {k: v for k, v in unparked.items() if k != "alerts"}
    expected["enabled"] = False
    assert parked == expected
    assert restarted == expected
    assert f"- name: {MC}\n  group: {MC}\n" in after_restart.text
    assert after_restart.pushes == []


def test_unpark_restores_alerts(tmp_path):
    rig = Rig(tmp_path, [FakeContainer(MC, status="created", labels=OFF)])
    rig.tick()
    rig.tick()
    # Label removed and docker compose up -d: a new, running container.
    rig.client.fleet = [FakeContainer(MC, status="running", cid="mc-3")]
    cycle = rig.tick()
    decl = cycle.ext(MC)
    assert decl is not None
    assert "enabled" not in decl
    assert decl["alerts"] == _alerts(MC)
    pushes = cycle.pushes_for(MC_KEY)
    assert len(pushes) == 1
    assert pushes[0].success is True
    assert pushes[0].error.startswith("state=running")
    assert "drift" not in pushes[0].error and REPARK_HINT not in pushes[0].error


def test_parked_to_live_same_cycle(tmp_path):
    mc = FakeContainer(MC, status="created", labels=OFF)
    rig = Rig(tmp_path, [mc])
    assert rig.tick().ext(MC).get("enabled") is False
    # docker compose up -d with the label still set starts the same container.
    mc.set_state("running")
    cycle = rig.tick()
    decl = cycle.ext(MC)
    assert "enabled" not in decl
    assert decl["alerts"] == _alerts(MC)
    pushes = cycle.pushes_for(MC_KEY)
    assert len(pushes) == 1
    assert pushes[0].success is False
    assert pushes[0].error.startswith(f"{DRIFT_PREFIX}running")
    assert " | state=running " in pushes[0].error


def test_live_respects_snooze_and_alerts_none(tmp_path):
    snoozed = FakeContainer(
        "young", labels={**OFF, "autogatus.snooze": "1h"}, started_at=started_now()
    )
    silenced = FakeContainer("hushed", labels={**OFF, "autogatus.alerts": "none"})
    rig = Rig(tmp_path, [snoozed, silenced])
    cycle = rig.tick()
    for name in ("young", "hushed"):
        decl = cycle.ext(name)
        assert decl is not None
        assert "alerts" not in decl, name
        assert "enabled" not in decl, name
        pushes = cycle.pushes_for(f"{name}_{name}")
        assert len(pushes) == 1
        assert pushes[0].success is False
        assert pushes[0].error.startswith(f"{DRIFT_PREFIX}running")


def test_parked_to_ran_on_failed_start(tmp_path):
    mc = FakeContainer(MC, status="created", labels=OFF)
    rig = Rig(tmp_path, [mc])
    assert rig.tick().ext(MC).get("enabled") is False
    # docker compose up -d, and the start fails: still created, Error set.
    mc.set_state("created", error=FAILED_START, exit_code=127)
    cycle = rig.tick()
    decl = cycle.ext(MC)
    assert "enabled" not in decl
    assert decl["alerts"] == _alerts(MC)
    pushes = cycle.pushes_for(MC_KEY)
    assert len(pushes) == 1
    assert pushes[0].success is False
    assert pushes[0].error.startswith("created | state=created")
    assert pushes[0].error.endswith(f" | {REPARK_HINT}")


def test_ran_declared_and_pages_on_fresh_monitor(tmp_path):
    mc = FakeContainer(MC, status="exited", labels=OFF, exit_code=0)
    rig = Rig(tmp_path, [mc])
    cycle = rig.tick()
    decl = cycle.ext(MC)
    assert decl is not None
    assert "enabled" not in decl
    assert decl["alerts"] == _alerts(MC)
    pushes = cycle.pushes_for(MC_KEY)
    assert len(pushes) == 1
    assert pushes[0].success is False
    assert pushes[0].error.startswith("exited (0) | state=exited")
    assert REPARK_HINT in pushes[0].error


def test_force_recreate_reparks_and_stops_pushing(tmp_path):
    rig = Rig(tmp_path, [FakeContainer(MC, status="running", labels=OFF)])
    live = rig.tick()
    assert live.pushes_for(MC_KEY)[0].error.startswith(DRIFT_PREFIX)
    # docker compose up --no-start --force-recreate: a new id, created.
    rig.client.fleet = [FakeContainer(MC, status="created", labels=OFF, cid="mc-new")]
    cycle = rig.tick()
    decl = cycle.ext(MC)
    assert decl.get("enabled") is False
    assert "alerts" not in decl
    assert cycle.pushes_for(MC_KEY) == []


def test_parked_checks_frozen_and_due_on_unpark(tmp_path, spy):
    checks = {"autogatus.check.cli.exec": "rcon-cli list", "autogatus.check.port.tcp": "25565"}
    mc = FakeContainer(MC, status="created", labels={**OFF, **checks})
    rig = Rig(tmp_path, [mc], exec_enabled=True)
    check_keys = [f"{MC}_{MC}-cli", f"{MC}_{MC}-port"]
    for _ in range(2):
        cycle = rig.tick()
        for name in (f"{MC}-cli", f"{MC}-port"):
            decl = cycle.ext(name)
            assert decl is not None
            assert decl.get("enabled") is False
            assert "alerts" not in decl
        for key in check_keys:
            assert cycle.pushes_for(key) == []
            assert rig.store is not None
            assert rig.store.get(key)["verdict"].error == PARKED_TEXT
    assert spy.calls == []
    assert mc.exec_calls == []

    # Unparked: label removed and docker compose up -d.
    rig.client.fleet = [FakeContainer(MC, status="running", labels=checks, cid="mc-2")]
    cycle = rig.tick()
    assert sorted(c[0] for c in spy.calls) == sorted(check_keys)
    for key in check_keys:
        assert len(cycle.pushes_for(key)) == 1
    for name in (f"{MC}-cli", f"{MC}-port"):
        decl = cycle.ext(name)
        assert "enabled" not in decl
        assert decl["alerts"]


@pytest.mark.parametrize("way", ["enable-false", "exclude"])
def test_parked_checks_frozen_when_liveness_opted_out(tmp_path, spy, way):
    labels = {**OFF, "autogatus.check.cli.exec": "rcon-cli list"}
    excludes: list[str] = []
    if way == "enable-false":
        labels["autogatus.enable"] = "false"
    else:
        excludes = [MC]
    rig = Rig(tmp_path, [FakeContainer(MC, status="created", labels=labels)], excludes=excludes)
    for _ in range(2):
        cycle = rig.tick()
        assert cycle.ext(MC) is None
        decl = cycle.ext(f"{MC}-cli")
        assert decl is not None
        assert decl.get("enabled") is False
        assert "alerts" not in decl
        assert cycle.pushes == []
    assert spy.calls == []


def test_tier1_alerts_dropped_and_still_enabled_while_parked(tmp_path):
    labels = {
        **OFF,
        "gatus.enable": "true",
        "gatus.web.url": "http://minecraft:8123/",
        "gatus.web.alerts": "custom",
    }
    mc = FakeContainer(MC, status="created", labels=labels)
    rig = Rig(tmp_path, [mc])

    parked = rig.tick().tier1("web")
    assert parked is not None
    assert "alerts" not in parked
    assert "enabled" not in parked

    mc.set_state("running")
    live = rig.tick().tier1("web")
    assert live["alerts"] == [{"type": "custom", "description": "web is down"}]
    assert "enabled" not in live

    mc.set_state("exited", exit_code=0)
    ran = rig.tick().tier1("web")
    assert ran["alerts"] == [{"type": "custom", "description": "web is down"}]
    assert "enabled" not in ran


def test_drift_without_liveness_warns_once(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="autogatus")
    rig = Rig(tmp_path, [FakeContainer(MC, status="running", labels=OFF)], excludes=[MC])
    cycles = [rig.tick(), rig.tick()]
    assert all(c.ext(MC) is None for c in cycles)
    drift = [m for m in _messages(caplog, logging.WARNING) if "flagged as drift" in m]
    assert drift == [
        "minecraft is running but declared offline (autogatus.offline=true), flagged as drift"
    ]

    # It stops being live (re-parked by a recreate): no WARN at all that cycle.
    caplog.clear()
    rig.client.fleet = [FakeContainer(MC, status="created", labels=OFF, cid="mc-2")]
    rig.tick()
    assert _messages(caplog, logging.WARNING) == []


def test_drift_with_monitoring_off_warns_once(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="autogatus")
    rig = Rig(tmp_path, [FakeContainer(MC, status="running", labels=OFF)], monitoring=False)
    rig.tick()
    rig.tick()
    drift = [m for m in _messages(caplog, logging.WARNING) if "flagged as drift" in m]
    assert drift == [
        "minecraft is running but declared offline (autogatus.offline=true), flagged as drift"
    ]


def _fleet(offline_value):
    def labels(extra=None):
        out = dict(extra or {})
        if offline_value is not None:
            out[OFFLINE_LABEL] = offline_value
        return out

    return [
        FakeContainer("stopped", status="exited", labels=labels()),
        FakeContainer("made", status="created", labels=labels({"autogatus.check.a.exec": "true"})),
        FakeContainer("up", status="running", labels=labels()),
        FakeContainer(
            "probe",
            status="created",
            labels=labels({"gatus.enable": "true", "gatus.p.url": "http://probe/"}),
        ),
    ]


def _records(tmp_path, offline_value):
    rig = Rig(tmp_path / str(offline_value), _fleet(offline_value))
    out = []
    for _ in range(2):
        cycle = rig.tick()
        out.append((normalize(cycle.text), [vars(p) for p in cycle.pushes]))
    return out, rig


def test_invalid_value_behaves_undeclared_and_warns_once(tmp_path, caplog, spy):
    caplog.set_level(logging.INFO, logger="autogatus")
    bad, bad_rig = _records(tmp_path, "maybe")
    warnings = _messages(caplog, logging.WARNING)
    caplog.clear()
    plain, _ = _records(tmp_path, None)
    assert bad == plain
    # A never-seen stopped container is skipped, as it is with no label.
    text = bad[-1][0]
    assert "name: stopped" not in text
    assert "enabled" not in text
    bad_lines = [m for m in warnings if m.startswith("bad autogatus.offline=")]
    assert sorted(bad_lines) == sorted(
        f"bad autogatus.offline=maybe on {n}, treating it as not offline"
        for n in ("stopped", "made", "up", "probe")
    )


def test_no_label_output_unchanged(tmp_path):
    records = run_no_label_fleet(tmp_path)
    golden = GOLDEN.read_text()
    assert dump_golden(records) == golden
    for cycle in json.loads(golden)["cycles"]:
        assert "enabled" not in cycle["file"]


def test_list_failure_keeps_frozen_endpoint(tmp_path, caplog):
    rig = Rig(tmp_path, [FakeContainer(MC, status="created", labels=OFF)])
    before = rig.tick()
    assert before.ext(MC).get("enabled") is False
    raw_before = pathlib.Path(rig.output).read_bytes()

    rig.client.list_error = RuntimeError("Cannot connect to the Docker daemon")
    caplog.set_level(logging.INFO, logger="autogatus")
    after = rig.tick()
    assert pathlib.Path(rig.output).read_bytes() == raw_before
    assert after.ext(MC).get("enabled") is False
    assert after.pushes == []
    assert any(
        m.startswith("docker listing failed") and "keeping last config" in m
        for m in _messages(caplog, logging.WARNING)
    )


def test_cycle_summary_skips_frozen_and_counts_drift_failure(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="autogatus")
    parked = FakeContainer("parked", status="created", labels=OFF)
    live = FakeContainer("live", status="running", labels=OFF)
    rig = Rig(tmp_path, [parked, live])
    first = rig.tick()
    assert first.pushed_keys() == ["live_live"]
    summaries = [m for m in _messages(caplog, logging.INFO) if m.startswith("reconcile:")]
    assert summaries == ["reconcile: 1 monitored, 1 pushed, 0 failed, config written"]

    caplog.clear()
    rig.pusher.fail_keys = {"live_live"}
    second = rig.tick()
    assert second.pushed_keys() == ["live_live"]
    assert second.pushes[0].error.startswith(DRIFT_PREFIX)
    summaries = [m for m in _messages(caplog, logging.INFO) if m.startswith("reconcile:")]
    assert summaries == ["reconcile: 1 monitored, 0 pushed, 1 failed"]
