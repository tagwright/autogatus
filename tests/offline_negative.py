# SPDX-License-Identifier: Apache-2.0
"""Committed negative fixtures for the offline-label guards.

Each is a known-bad variant of the shipped code. tests/test_offline_guards.py
feeds each one to its guard and asserts the guard goes red, so the proof that
the guard can catch its violation re-runs on every CI.
"""

from __future__ import annotations

from autogatus.monitor import ContainerMonitor, _gatus_key
from autogatus.offline import (
    DECLARED,
    LIVE,
    LIVE_STATES,
    NONE,
    PARKED,
    UNDECLARED,
    is_declared,
    parked_verdict,
)


def parse_declares_any_nonempty(value) -> str:
    """For guard_invalid_never_silences: a parser that declares on any
    non-empty value, so a typo silences the container."""
    if value is None or str(value).strip() == "":
        return UNDECLARED
    return DECLARED


def classify_quiet_when_not_live(labels, attrs) -> str:
    """For guard_ran_is_never_parked: the rejected OFL-O3 A rule, quiet
    whenever the container is not live, so a crash after a start is parked."""
    if not is_declared(labels):
        return NONE
    state = (attrs or {}).get("State") or {}
    return LIVE if state.get("Status") in LIVE_STATES else PARKED


class TodaysGateMonitor(ContainerMonitor):
    """For guard_declared_always_declared: eligibility that ignores the label,
    which is the one-shot rule as it stood before the offline label."""

    def _eligible(self, container, oclass: str) -> bool:
        return container.name in self._seen_running


class DisablesAllDeclaredMonitor(ContainerMonitor):
    """For guard_disabled_only_when_parked: disables every declared-offline
    container's endpoints, whatever its class."""

    @staticmethod
    def _frozen(oclass: str) -> bool:
        return oclass != NONE


class PushesParkedMonitor(ContainerMonitor):
    """For guard_frozen_never_pushed: keeps pushing the parked verdict to every
    endpoint it declares disabled."""

    def reconcile(self, allowlist=None):
        declarations, verdicts = super().reconcile(allowlist=allowlist)
        extra = [
            (_gatus_key(d["group"], d["name"]), parked_verdict())
            for d in declarations
            if d.get("enabled") is False
        ]
        return declarations, verdicts + extra
