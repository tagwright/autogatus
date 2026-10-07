# SPDX-License-Identifier: Apache-2.0
"""The ``autogatus.offline`` label: a container that is stopped on purpose.

An operator declares a service offline in that service's own compose file and
parks it with ``docker compose up --no-start <svc>``, which recreates the
container with the label and does not start it. This module is the pure core of
that rule: the label grammar, a classifier from a container's labels and its
``docker inspect`` attrs to one of four classes, and the verdict texts. No
Docker import, no I/O, no clock, so it is tested over its whole input space.

The classes:

- ``none``: not declared offline. Every rule is the ordinary one.
- ``parked``: declared offline, and the container has never been started since
  it was created. Its status is ``created``, its ``StartedAt`` is empty or the
  zero time, and its ``State.Error`` is empty (a start that was tried and failed
  leaves an error). Its endpoints are frozen: declared, disabled, no alerts,
  nothing pushed.
- ``live``: declared offline, and the container is running, restarting or
  paused. That is drift, and it pages under the container's normal alerts.
- ``ran``: declared offline and neither of the above, for example exited, dead,
  or created with a ``State.Error``. The container has run, or tried to, since it
  was created, so it is monitored normally with a hint on how to re-park it.
"""

from __future__ import annotations

from .health import Verdict

OFFLINE_LABEL = "autogatus.offline"

# Value grammar, the same words as the other autogatus and gatus bool labels.
_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})

# parse_offline results.
DECLARED = "declared"
UNDECLARED = "undeclared"
INVALID = "invalid"

# classify results.
NONE = "none"
PARKED = "parked"
LIVE = "live"
RAN = "ran"

# Docker statuses that hold a process. paused counts, it still has one.
LIVE_STATES = frozenset({"running", "restarting", "paused"})

_ZERO_TIME = "0001-01-01"

PARKED_TEXT = "offline as declared (autogatus.offline=true) | state=created"
DRIFT_PREFIX = "drift: declared offline (autogatus.offline=true) but the container is "
REPARK_HINT = (
    "not parked: started since it was created, "
    "re-park with docker compose up --no-start --force-recreate"
)

# Log lines, logged once each (see reconcile.OfflineNotes).
PARKED_LOG = "%s is parked (autogatus.offline=true), its endpoints are frozen"
LIVE_LOG = "%s is %s but declared offline (autogatus.offline=true), flagged as drift"
RAN_LOG = "%s is declared offline but has run since it was created, monitoring it normally"
BAD_VALUE_LOG = "bad autogatus.offline=%s on %s, treating it as not offline"


def parse_offline(value) -> str:
    """Read one ``autogatus.offline`` value.

    Truthy words declare the container offline. Falsy words, an empty value or
    no label at all do not. Anything else is ``invalid``, which also does not
    declare, so a typo keeps paging.
    """
    if value is None:
        return UNDECLARED
    word = str(value).strip().lower()
    if word == "" or word in _FALSE:
        return UNDECLARED
    if word in _TRUE:
        return DECLARED
    return INVALID


def is_declared(labels) -> bool:
    return parse_offline((labels or {}).get(OFFLINE_LABEL)) == DECLARED


def _never_started(started_at) -> bool:
    if started_at is None:
        return True
    text = str(started_at).strip()
    return text == "" or text.startswith(_ZERO_TIME)


def classify(labels, attrs) -> str:
    """One of ``none``, ``parked``, ``live`` or ``ran`` for a container.

    ``attrs`` is the container's ``docker inspect`` output. Only a container
    that is declared offline is ever anything but ``none``, and only one whose
    state shows it was never started is ``parked``, so anything this cannot
    read stays monitored.
    """
    if not is_declared(labels):
        return NONE
    state = attrs.get("State") if isinstance(attrs, dict) else None
    if not isinstance(state, dict):
        state = {}
    status = state.get("Status")
    if status in LIVE_STATES:
        return LIVE
    if status == "created" and _never_started(state.get("StartedAt")) and not state.get("Error"):
        return PARKED
    return RAN


def parked_verdict() -> Verdict:
    """What the store and ``/details`` show for a parked endpoint. It is never
    pushed, so Gatus never sees this text."""
    return Verdict(False, PARKED_TEXT, None, ["offline as declared"])


def _reasons_and_status(real: Verdict) -> tuple[str, str]:
    """Split a liveness verdict into its failure reasons and its status line.
    The status line is always last and never holds the separator."""
    if real.success:
        return "", real.error
    reasons, sep, status = real.error.rpartition(" | ")
    if not sep:
        return real.error, ""
    return reasons, status


def drift_verdict(real: Verdict, state: str) -> Verdict:
    """A failing verdict for a live container that is declared offline. It
    wraps the real verdict and keeps its failure reasons and status line."""
    reasons, status = _reasons_and_status(real)
    text = f"{DRIFT_PREFIX}{state}"
    if reasons:
        text += f"; {reasons}"
    if status:
        text += f" | {status}"
    return Verdict(False, text, real.headline, ["drift", *real.reasons])


def ran_verdict(real: Verdict) -> Verdict:
    """The real verdict, failing, with the re-park hint after the real error."""
    return Verdict(False, f"{real.error} | {REPARK_HINT}", real.headline, [*real.reasons])
