# SPDX-License-Identifier: Apache-2.0
"""Level 1: the pure offline-label core, tested over its input space.

Every test here is red under a named one-line mutation of autogatus/offline.py
(recorded in the build report), so none of them passes vacuously.
"""

from __future__ import annotations

import itertools

from autogatus import offline
from autogatus.health import ContainerHealth, Thresholds, evaluate
from autogatus.offline import (
    DECLARED,
    DRIFT_PREFIX,
    INVALID,
    LIVE,
    NONE,
    OFFLINE_LABEL,
    PARKED,
    RAN,
    REPARK_HINT,
    UNDECLARED,
    classify,
    drift_verdict,
    parked_verdict,
    parse_offline,
    ran_verdict,
)

TRUTHY = ["true", "1", "yes", "on"]
FALSY = ["false", "0", "no", "off"]
GARBAGE = [
    "maybe",
    "y",
    "n",
    "t",
    "f",
    "2",
    "-1",
    "01",
    "truee",
    "ture",
    "tru e",
    "yes please",
    "true false",
    "offline",
    "parked",
    "enabled",
    "none",
    "null",
    "0x1",
    "on;",
    "'true'",
    '"true"',
    "ıtrue",
    "１",
    "-",
    ".",
]
PADDING = ["", " ", "\t", "  \n", "\r\n"]


def _cases(word: str) -> list[str]:
    mixed = "".join(ch.upper() if i % 2 else ch.lower() for i, ch in enumerate(word))
    return sorted({word.lower(), word.upper(), word.capitalize(), mixed})


def _padded(word: str) -> list[str]:
    return [f"{a}{w}{b}" for w in _cases(word) for a in PADDING for b in PADDING]


def test_parse_offline_partition():
    for word in TRUTHY:
        for value in _padded(word):
            assert parse_offline(value) == DECLARED, repr(value)
    for word in FALSY:
        for value in _padded(word):
            assert parse_offline(value) == UNDECLARED, repr(value)
    for value in [None, *PADDING]:
        assert parse_offline(value) == UNDECLARED, repr(value)
    for word in GARBAGE:
        for value in _padded(word):
            assert parse_offline(value) == INVALID, repr(value)

    # The invariant: nothing maps to declared unless it is a truthy word.
    corpus = [None, *PADDING]
    for word in TRUTHY + FALSY + GARBAGE:
        corpus.extend(_padded(word))
    for value in corpus:
        if parse_offline(value) == DECLARED:
            assert str(value).strip().lower() in TRUTHY, repr(value)
        if value is not None and str(value).strip().lower() not in TRUTHY:
            assert parse_offline(value) != DECLARED, repr(value)


STATUSES = ["created", "running", "restarting", "paused", "exited", "dead", "removing", None]
STARTED = {
    "missing": None,
    "empty": "",
    "zero": "0001-01-01T00:00:00Z",
    "real": "2026-10-06T20:57:01.123456789Z",
}
ERRORS = ["", "exec: /nonexistent: no such file or directory"]


def _attrs(status, started, error) -> dict:
    state: dict = {"ExitCode": 0}
    if status is not None:
        state["Status"] = status
    if started is not None:
        state["StartedAt"] = started
    state["Error"] = error
    return {"State": state}


def _expected(status, started_kind, error) -> str:
    if status == "created" and started_kind in ("missing", "empty", "zero") and not error:
        return PARKED
    if status in ("running", "restarting", "paused"):
        return LIVE
    return RAN


def test_classify_partition():
    declared = {OFFLINE_LABEL: "true"}
    undeclared_labels = [{}, {OFFLINE_LABEL: "false"}, {OFFLINE_LABEL: "maybe"}, {"other": "1"}]
    for status, (kind, started), error in itertools.product(STATUSES, STARTED.items(), ERRORS):
        attrs = _attrs(status, started, error)
        want = _expected(status, kind, error)
        got = classify(declared, attrs)
        assert got == want, (status, kind, error, got)
        for labels in undeclared_labels:
            assert classify(labels, attrs) == NONE, (labels, status, kind, error)
    assert classify(None, _attrs("created", "", "")) == NONE


def test_classify_live_set():
    declared = {OFFLINE_LABEL: "yes"}
    for status in ("running", "restarting", "paused"):
        for started in STARTED.values():
            assert classify(declared, _attrs(status, started, "")) == LIVE, (status, started)
    assert offline.LIVE_STATES == {"running", "restarting", "paused"}


def test_classify_missing_state_is_not_parked():
    declared = {OFFLINE_LABEL: "true"}
    shapes = [
        None,
        {},
        {"Id": "abc"},
        {"State": None},
        {"State": "created"},
        {"State": {}},
        {"State": {"StartedAt": "0001-01-01T00:00:00Z", "Error": ""}},
        {"State": {"StartedAt": "", "ExitCode": 0}},
    ]
    for attrs in shapes:
        assert classify(declared, attrs) != PARKED, attrs


def test_parked_verdict():
    v = parked_verdict()
    assert v.success is False
    assert v.error.startswith("offline as declared (autogatus.offline=true)")
    assert "state=created" in v.error
    assert v.error == "offline as declared (autogatus.offline=true) | state=created"
    assert v.headline is None


def _health(**kw) -> ContainerHealth:
    base: dict = {"name": "mc", "stack": "mc", "state": "running", "restart_count": 0}
    base.update(kw)
    return ContainerHealth(**base)


def test_drift_verdict_keeps_real_status():
    # A real verdict with several failure reasons.
    real = evaluate(
        _health(
            mem_used=980.0,
            mem_limit=1000.0,
            mem_percent=98.0,
            health_status="unhealthy",
            health_output="probe | timed out",
        ),
        Thresholds(),
    )
    assert real.success is False and len(real.reasons) == 2
    status_line = real.error.rpartition(" | ")[2]
    assert status_line.startswith("state=running")

    v = drift_verdict(real, "running")
    assert v.success is False
    assert v.error.startswith(f"{DRIFT_PREFIX}running")
    assert v.error.startswith(
        "drift: declared offline (autogatus.offline=true) but the container is"
    )
    for reason in real.reasons:
        assert reason in v.error, reason
    assert v.error == f"{DRIFT_PREFIX}running; {'; '.join(real.reasons)} | {status_line}"
    assert v.headline == real.headline

    # A healthy real verdict has no reasons, only the status line.
    ok = evaluate(_health(state="paused"), Thresholds())
    ok_running = evaluate(_health(), Thresholds())
    assert ok_running.success is True
    v_ok = drift_verdict(ok_running, "running")
    assert v_ok.success is False
    assert v_ok.error == f"{DRIFT_PREFIX}running | {ok_running.error}"

    # A paused container fails evaluate on its state, and that reason is kept.
    v_paused = drift_verdict(ok, "paused")
    assert v_paused.error.startswith(f"{DRIFT_PREFIX}paused; paused | state=paused")


def test_ran_verdict_appends_hint():
    real = evaluate(_health(state="exited", exit_code=137), Thresholds())
    assert real.success is False
    v = ran_verdict(real)
    assert v.success is False
    assert v.error.startswith(real.error)
    assert v.error == f"{real.error} | {REPARK_HINT}"
    assert v.error.endswith(
        " | not parked: started since it was created, "
        "re-park with docker compose up --no-start --force-recreate"
    )
    assert v.headline == real.headline
