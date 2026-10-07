# SPDX-License-Identifier: Apache-2.0
"""Level 3: guards on the offline label, each with a committed negative fixture.

The guarded class is the Testing Standard's first: a failure fails closed and
surfaces, never silently proceeds. For a monitoring tool, failing closed means
it keeps alerting and keeps Gatus's heartbeat check on. Each guard is a function
that takes the code under test as a parameter. One test runs it on the shipped
code and passes, the other runs it on the known-bad variant committed in
tests/offline_negative.py and asserts the guard goes red.
"""

from __future__ import annotations

import itertools
from unittest import mock

import pytest
from fakes import Cycle, FakeContainer, Rig
from offline_negative import (
    DisablesAllDeclaredMonitor,
    PushesParkedMonitor,
    TodaysGateMonitor,
    classify_quiet_when_not_live,
    parse_declares_any_nonempty,
)

from autogatus import offline
from autogatus.health import Verdict
from autogatus.monitor import ContainerMonitor, _gatus_key
from autogatus.offline import OFFLINE_LABEL, PARKED

FAILED_START = 'exec: "/nonexistent": stat /nonexistent: no such file or directory'
REAL_START = "2026-10-06T20:57:01.123456789Z"
ALL_STATUSES = ["created", "running", "restarting", "paused", "exited", "dead", "removing", None]

NON_TRUTHY = [
    "false",
    "FALSE",
    " no ",
    "Off",
    "0",
    "",
    " ",
    "maybe",
    "ture",
    "y",
    "2",
    "none",
    "offline",
    "true false",
    "'true'",
    "１",
]
TRUTHY = ["true", "TRUE", " True ", "1", "yes", "Yes", "on", " ON\n"]


def _owned(cycle: Cycle, name: str):
    """Every declaration the cycle's file holds for container ``name``: its
    liveness endpoint, its checks, and its tier-1 endpoints."""
    ext = [d for d in cycle.external if d["name"] == name or d["name"].startswith(f"{name}-")]
    tier1 = [d for d in cycle.endpoints if d["group"] == name]
    return ext, tier1


# --- guard_invalid_never_silences --------------------------------------------


def guard_invalid_never_silences(parse, tmp_path) -> None:
    """For every non-truthy value, every declaration ``_tick`` produces for that
    container stays enabled and keeps its alerts, in every Docker state,
    including the parked-shaped one."""
    with mock.patch.object(offline, "parse_offline", parse):
        for i, value in enumerate(NON_TRUTHY):
            name = f"c{i}"
            labels = {
                OFFLINE_LABEL: value,
                "gatus.enable": "true",
                "gatus.web.url": f"http://{name}/",
                "autogatus.check.c.exec": "true",
            }
            rig = Rig(tmp_path / name, [FakeContainer(name, "running", labels)])
            steps = [
                FakeContainer(name, "running", labels),
                FakeContainer(name, "created", labels, cid=f"{name}-2"),
                FakeContainer(name, "created", labels, cid=f"{name}-3", error=FAILED_START),
                FakeContainer(name, "exited", labels, cid=f"{name}-3"),
            ]
            for step in steps:
                rig.client.fleet = [step]
                cycle = rig.tick()
                ext, tier1 = _owned(cycle, name)
                assert len(ext) == 2 and len(tier1) == 1, (value, step.status, ext, tier1)
                for d in ext + tier1:
                    assert d.get("enabled", True) is not False, (value, step.status, d)
                    assert d.get("alerts"), (value, step.status, d)


def test_guard_invalid_never_silences_shipped(tmp_path):
    guard_invalid_never_silences(offline.parse_offline, tmp_path)


def test_guard_invalid_never_silences_catches_any_nonempty_parser(tmp_path):
    with pytest.raises(AssertionError):
        guard_invalid_never_silences(parse_declares_any_nonempty, tmp_path)


# --- guard_ran_is_never_parked -----------------------------------------------


def guard_ran_is_never_parked(classify) -> None:
    """Over status times StartedAt-set times Error, nothing that has run (a real
    StartedAt) or tried to (a State.Error) is parked."""
    declared = {OFFLINE_LABEL: "true"}
    # Not vacuous: a container that has never been started is parked.
    never = {"State": {"Status": "created", "StartedAt": "0001-01-01T00:00:00Z", "Error": ""}}
    assert classify(declared, never) == PARKED
    for status, error in itertools.product(ALL_STATUSES, ["", FAILED_START]):
        state = {"StartedAt": REAL_START, "Error": error}
        if status is not None:
            state["Status"] = status
        assert classify(declared, {"State": state}) != PARKED, (status, "started", error)
    for status, started in itertools.product(ALL_STATUSES, [None, "", "0001-01-01T00:00:00Z"]):
        state = {"Error": FAILED_START}
        if status is not None:
            state["Status"] = status
        if started is not None:
            state["StartedAt"] = started
        assert classify(declared, {"State": state}) != PARKED, (status, started, "error")


def test_guard_ran_is_never_parked_shipped():
    guard_ran_is_never_parked(offline.classify)


def test_guard_ran_is_never_parked_catches_quiet_when_not_live():
    with pytest.raises(AssertionError):
        guard_ran_is_never_parked(classify_quiet_when_not_live)


# --- guard_declared_always_declared ------------------------------------------

CLASS_SHAPES: dict[str, dict] = {
    "parked": {"status": "created"},
    "running": {"status": "running"},
    "restarting": {"status": "restarting"},
    "paused": {"status": "paused"},
    "exited": {"status": "exited", "exit_code": 1},
    "dead": {"status": "dead"},
    "removing": {"status": "removing"},
    "failedstart": {"status": "created", "error": FAILED_START, "exit_code": 127},
}


def guard_declared_always_declared(monitor_cls, tmp_path) -> None:
    """Every container with a truthy label is declared on a fresh monitor's
    first ``_tick``, in every class."""
    for (shape, kw), value in itertools.product(CLASS_SHAPES.items(), TRUTHY):
        name = f"{shape}-{TRUTHY.index(value)}"
        c = FakeContainer(name, labels={OFFLINE_LABEL: value}, **kw)
        rig = Rig(tmp_path / name, [c], monitor_cls=monitor_cls)
        cycle = rig.tick()
        assert cycle.ext(name) is not None, (shape, value)


def test_guard_declared_always_declared_shipped(tmp_path):
    guard_declared_always_declared(ContainerMonitor, tmp_path)


def test_guard_declared_always_declared_catches_todays_gate(tmp_path):
    with pytest.raises(AssertionError):
        guard_declared_always_declared(TodaysGateMonitor, tmp_path)


# --- guard_disabled_only_when_parked -----------------------------------------


def _extras(kind: str, name: str) -> dict:
    out: dict = {}
    if kind in ("checks", "both"):
        out["autogatus.check.x.exec"] = "true"
        out["autogatus.check.y.tcp"] = "1"
    if kind in ("tier1", "both"):
        out["gatus.enable"] = "true"
        out["gatus.web.url"] = f"http://{name}/"
    return out


def guard_disabled_only_when_parked(monitor_cls, tmp_path) -> None:
    """``enabled: false`` appears only on a parked container's liveness and
    check declarations, never on a live, ran or undeclared container's and never
    on a tier-1 endpoint. Over every class, with and without checks and tier-1."""
    shapes = {"none-running": {"status": "running"}, **CLASS_SHAPES}
    fleet = []
    parked_names = set()
    for (shape, kw), kind in itertools.product(
        shapes.items(), ["plain", "checks", "tier1", "both"]
    ):
        name = f"{shape}-{kind}"
        labels = _extras(kind, name)
        if shape != "none-running":
            labels[OFFLINE_LABEL] = "true"
        fleet.append(FakeContainer(name, labels=labels, **kw))
        if shape == "parked":
            parked_names.add(name)
    # An undeclared container seen running and then stopped is declared too.
    seen = FakeContainer("none-stopped", labels=_extras("both", "none-stopped"))
    fleet.append(seen)

    rig = Rig(tmp_path, fleet, monitor_cls=monitor_cls)
    with mock.patch("autogatus.monitor.run_check", _ok_check):
        cycles = [rig.tick()]
        seen.set_state("exited", exit_code=1)
        cycles.append(rig.tick())

    disabled_seen = 0
    for cycle in cycles:
        for d in cycle.endpoints:
            assert "enabled" not in d, d
        for d in cycle.external:
            if d.get("enabled", True) is not False:
                continue
            disabled_seen += 1
            owners = [n for n in parked_names if d["name"] == n or d["name"].startswith(f"{n}-")]
            assert owners, d
    # Not vacuous: parked liveness and check endpoints were declared disabled.
    assert disabled_seen > 0


def _ok_check(check, container, registry=None, reg_key=None):
    return Verdict(True, f"{check.kind} ok", None)


def test_guard_disabled_only_when_parked_shipped(tmp_path):
    guard_disabled_only_when_parked(ContainerMonitor, tmp_path)


def test_guard_disabled_only_when_parked_catches_disable_all_declared(tmp_path):
    with pytest.raises(AssertionError):
        guard_disabled_only_when_parked(DisablesAllDeclaredMonitor, tmp_path)


# --- guard_frozen_never_pushed -----------------------------------------------


def guard_frozen_never_pushed(monitor_cls, tmp_path) -> None:
    """Over a scripted sequence through every transition, no key pushed in a
    cycle is declared ``enabled: false`` in that cycle's file."""
    name = "mc"
    check = {"autogatus.check.cli.exec": "true"}
    off = {OFFLINE_LABEL: "true", **check}

    def c(status, cid, labels=off, **kw):
        return FakeContainer(name, status, labels, cid=cid, **kw)

    live1 = c("created", "id-2")
    live2 = c("running", "id-6")
    script = [
        ("unlabeled running", [c("running", "id-1", labels=check)], None),
        ("parked", [live1], None),
        ("parked steady", [live1], None),
        ("parked to live", [live1], lambda: live1.set_state("running")),
        ("live to parked", [c("created", "id-3")], None),
        ("parked to ran (failed start)", [c("created", "id-3", error=FAILED_START)], None),
        ("ran to parked", [c("created", "id-4")], None),
        ("parked, autogatus restarted", [c("created", "id-4")], "restart"),
        ("parked to live", [c("running", "id-4")], None),
        ("live to ran (stopped)", [c("exited", "id-4", exit_code=0)], None),
        ("ran to parked", [c("created", "id-5")], None),
        ("parked to live (new id)", [live2], None),
        ("live, label removed", [c("running", "id-7", labels=check)], None),
    ]
    rig = Rig(tmp_path, monitor_cls=monitor_cls)
    frozen_cycles = 0
    pushed_cycles = 0
    with mock.patch("autogatus.monitor.run_check", _ok_check):
        for label, fleet, action in script:
            if action == "restart":
                rig.restart()
            elif callable(action):
                action()
            rig.client.fleet = fleet
            cycle = rig.tick()
            frozen = {
                _gatus_key(d["group"], d["name"])
                for d in cycle.external
                if d.get("enabled", True) is False
            }
            pushed = set(cycle.pushed_keys())
            assert not frozen & pushed, (label, sorted(frozen & pushed))
            frozen_cycles += bool(frozen)
            pushed_cycles += bool(pushed)
    # Not vacuous: the script froze endpoints and pushed to others.
    assert frozen_cycles >= 5 and pushed_cycles >= 5


def test_guard_frozen_never_pushed_shipped(tmp_path):
    guard_frozen_never_pushed(ContainerMonitor, tmp_path)


def test_guard_frozen_never_pushed_catches_pushes_parked(tmp_path):
    with pytest.raises(AssertionError):
        guard_frozen_never_pushed(PushesParkedMonitor, tmp_path)
