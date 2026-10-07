# SPDX-License-Identifier: Apache-2.0
"""Shared fakes for the wiring tests that drive ``__main__._tick``.

- ``FakeContainer``: a container with ``status`` and a full ``attrs["State"]``
  shaped like ``docker inspect`` output, plus ``stats`` and ``exec_run``.
- ``FakeClient``: a scripted Docker client. ``list_error`` is the fault knob on
  the listing, set it to an exception and every ``containers.list`` raises it.
- ``RecordingPusher``: records every push attempt. ``fail_keys`` is the fault
  knob on the push, a push to a key in it returns False as a failed HTTP push
  would.
- ``RunCheckSpy``: stands in for ``checks.run_check`` and records each call.
- ``Rig``: one autogatus process as ``run()`` builds it (a ``ContainerMonitor``
  and a ``Writer`` on a temp path), driven one cycle at a time through the real
  ``_tick``. ``restart()`` builds a new monitor and writer the way ``run()`` does
  on startup, keeping the client, pusher and output file.

Older test files keep their own fakes. These are for the offline-label tests
and the no-label golden.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import yaml

from autogatus import __main__ as entry
from autogatus import reconcile
from autogatus.health import Thresholds, Verdict
from autogatus.monitor import ContainerMonitor
from autogatus.reconcile import Writer
from autogatus.store import Store

ZERO_TIME = "0001-01-01T00:00:00Z"
OLD_START = "2026-01-01T00:00:00.000000000Z"

_LIVE = {"running", "restarting", "paused"}


def started_now() -> str:
    """A StartedAt of this instant, in Docker's nanosecond RFC3339 form."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


class FakeExecResult:
    def __init__(self, exit_code: int = 0, output: bytes = b"ok"):
        self.exit_code = exit_code
        self.output = output


class FakeContainer:
    """A container as docker-py hands it back from a non-sparse list."""

    def __init__(
        self,
        name: str,
        status: str = "running",
        labels: dict | None = None,
        *,
        started_at: str | None = None,
        error: str = "",
        exit_code: int | None = None,
        restart_count: int = 0,
        cid: str | None = None,
        oom: bool = False,
    ):
        self.name = name
        self.id = cid or f"{name}-id"
        self.labels = dict(labels or {})
        self.exec_calls: list = []
        self.attrs: dict = {}
        self.set_state(
            status,
            started_at=started_at,
            error=error,
            exit_code=exit_code,
            restart_count=restart_count,
            oom=oom,
        )

    def set_state(
        self,
        status: str,
        *,
        started_at: str | None = None,
        error: str = "",
        exit_code: int | None = None,
        restart_count: int | None = None,
        oom: bool = False,
    ) -> None:
        if started_at is None:
            started_at = ZERO_TIME if status == "created" else OLD_START
        if exit_code is None:
            exit_code = 0
        if restart_count is None:
            restart_count = self.attrs.get("RestartCount", 0)
        self.attrs = {
            "Id": self.id,
            "State": {
                "Status": status,
                "Running": status in _LIVE,
                "Paused": status == "paused",
                "Restarting": status == "restarting",
                "OOMKilled": oom,
                "Dead": status == "dead",
                "Pid": 4242 if status in _LIVE else 0,
                "ExitCode": exit_code,
                "Error": error,
                "StartedAt": started_at,
                "FinishedAt": ZERO_TIME if status in _LIVE or status == "created" else OLD_START,
            },
            "RestartCount": restart_count,
        }

    @property
    def status(self):
        # Same as docker-py's Container.status.
        state = self.attrs["State"]
        if isinstance(state, dict):
            return state["Status"]
        return state

    def stats(self, stream=False):
        # 64 MiB used of a 512 MiB limit, so the headline and status line carry
        # real numbers. CPU needs two samples, so it reads as unknown.
        return {"memory_stats": {"usage": 64 * 1024 * 1024, "limit": 512 * 1024 * 1024}}

    def exec_run(self, cmd, demux=False):
        self.exec_calls.append(cmd)
        return FakeExecResult()


class _Collection:
    def __init__(self, client: FakeClient):
        self._client = client

    def list(self, all=False, ignore_removed=False):
        self._client.list_calls += 1
        if self._client.list_error is not None:
            raise self._client.list_error
        return list(self._client.fleet)


class FakeClient:
    def __init__(self, fleet=()):
        self.fleet = list(fleet)
        # Fault knob: an exception raised by every containers.list call.
        self.list_error: Exception | None = None
        self.list_calls = 0
        self.containers = _Collection(self)


@dataclass
class Push:
    key: str
    success: bool
    error: str
    duration: object


class RecordingPusher:
    def __init__(self):
        self.pushes: list[Push] = []
        # Fault knob: a push to one of these keys fails.
        self.fail_keys: set[str] = set()

    def push(self, key, success, error="", duration=None):
        self.pushes.append(Push(key, success, error, duration))
        return key not in self.fail_keys


class RunCheckSpy:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, check, container, registry=None, reg_key=None):
        self.calls.append((reg_key, check.kind, container.name))
        return Verdict(True, f"{check.kind} ok", None)


_STAMP = re.compile(r"rendered: \S+")


def normalize(text: str) -> str:
    """The generated file without its timestamp."""
    return _STAMP.sub("rendered: <stamp>", text)


@dataclass
class Cycle:
    """What one ``_tick`` produced: the file on disk after it and its pushes."""

    text: str
    pushes: list[Push] = field(default_factory=list)

    @property
    def doc(self) -> dict:
        return yaml.safe_load(self.text) or {}

    @property
    def endpoints(self) -> list:
        return self.doc.get("endpoints") or []

    @property
    def external(self) -> list:
        return self.doc.get("external-endpoints") or []

    def ext(self, name: str) -> dict | None:
        found = [d for d in self.external if d["name"] == name]
        assert len(found) <= 1, f"{name} declared {len(found)} times"
        return found[0] if found else None

    def tier1(self, name: str) -> dict | None:
        found = [d for d in self.endpoints if d["name"] == name]
        return found[0] if found else None

    def pushed_keys(self) -> list[str]:
        return [p.key for p in self.pushes]

    def pushes_for(self, key: str) -> list[Push]:
        return [p for p in self.pushes if p.key == key]


class Rig:
    """One autogatus process, built the way ``run()`` builds it, ticked by hand."""

    def __init__(
        self,
        tmp_path,
        fleet=(),
        *,
        excludes=(),
        exec_enabled: bool = True,
        stack_map: dict | None = None,
        monitor_cls=ContainerMonitor,
        monitoring: bool = True,
    ):
        self.client = FakeClient(fleet)
        self.pusher = RecordingPusher()
        self.output = str(tmp_path / "out" / "autogatus.yaml")
        self.excludes = list(excludes)
        self.exec_enabled = exec_enabled
        self.stack_map = dict(stack_map or {})
        self.monitor_cls = monitor_cls
        self.monitoring = monitoring
        self.store: Store | None = None
        self.monitor: ContainerMonitor | None = None
        self.writer: Writer
        self.restart()

    def restart(self) -> None:
        """A new monitor, store and writer, as run() makes them on startup. The
        process-wide offline log notes start empty too, as in a new process."""
        notes = getattr(reconcile, "offline_notes", None)
        if notes is not None:
            notes.reset()
        self.writer = Writer(self.output)
        if not self.monitoring:
            self.store = None
            self.monitor = None
            return
        self.store = Store()
        self.monitor = self.monitor_cls(
            client=self.client,
            pusher=self.pusher,
            token="tok",
            stack_map=self.stack_map,
            thresholds=Thresholds(mem_percent=95.0, cpu_percent=None),
            excludes=self.excludes,
            headline_metric="mem_used_mb",
            heartbeat_interval="90s",
            default_group="",
            store=self.store,
            default_alert_types=["custom"],
            exec_enabled=self.exec_enabled,
            resync_interval=15,
            failure_latch=3,
        )

    def read(self) -> str:
        try:
            with open(self.output) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def tick(self) -> Cycle:
        self.pusher.pushes = []
        entry._tick(self.client, self.writer, self.monitor)
        return Cycle(self.read(), list(self.pusher.pushes))


def cycle_record(cycle: Cycle) -> dict:
    """A JSON-able record of one cycle, for the golden."""
    return {
        "file": normalize(cycle.text),
        "pushes": [[p.key, p.success, p.error, p.duration] for p in cycle.pushes],
    }


def no_label_fleet() -> list[FakeContainer]:
    """A mixed fleet that carries no autogatus.offline label anywhere."""
    return [
        FakeContainer(
            "web",
            labels={
                "com.docker.compose.service": "web",
                "gatus.enable": "true",
                "gatus.site.url": "http://web:80/health",
                "gatus.site.alerts": "custom",
            },
        ),
        FakeContainer("worker"),
        FakeContainer("job", status="exited", exit_code=0),
        FakeContainer("made", status="created"),
        FakeContainer("crashy"),
        FakeContainer("quiet", labels={"autogatus.alerts": "none"}),
        FakeContainer(
            "structured",
            labels={
                "autogatus.alerts.0.type": "custom",
                "autogatus.alerts.0.failure-threshold": "5",
                "autogatus.alerts.0.send-on-resolved": "true",
            },
        ),
        FakeContainer(
            "optout",
            labels={"autogatus.enable": "false", "autogatus.check.ping.exec": "true"},
        ),
        FakeContainer("skipme", labels={"autogatus.check.port.exec": "true"}),
        FakeContainer("sleepy", labels={"autogatus.snooze": "2m"}),
        FakeContainer("fresh", labels={"autogatus.snooze": "1h"}, started_at=started_now()),
        FakeContainer("restarter"),
        FakeContainer("pauser"),
        FakeContainer(
            "probed",
            status="exited",
            exit_code=1,
            labels={"gatus.enable": "true", "gatus.api.url": "tcp://probed:9"},
        ),
    ]


def run_no_label_fleet(tmp_path) -> list[dict]:
    """Two cycles over ``no_label_fleet``. Between them crashy exits, restarter
    bumps its restart count and pauser pauses."""
    fleet = no_label_fleet()
    rig = Rig(tmp_path, fleet, excludes=["skipme"], stack_map={"web": "apps"})
    records = [cycle_record(rig.tick())]
    by_name = {c.name: c for c in fleet}
    by_name["crashy"].set_state("exited", exit_code=1)
    by_name["restarter"].set_state("running", restart_count=1)
    by_name["pauser"].set_state("paused")
    records.append(cycle_record(rig.tick()))
    return records


def dump_golden(records) -> str:
    return json.dumps({"cycles": records}, indent=2, sort_keys=True) + "\n"
