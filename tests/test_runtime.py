"""Runtime robustness gates: docker-failure handling, reconnect state, durable
writes, interval floor, and stopped-container tier-1 behavior."""

import pytest

from autogatus import __main__ as agmain
from autogatus.duration import parse_duration_seconds
from autogatus.health import Thresholds
from autogatus.monitor import ContainerMonitor
from autogatus.reconcile import DockerListError, Writer, _atomic_write, _fsync_dir, gather_endpoints

TIER1 = {
    "gatus.enable": "true",
    "gatus.web.url": "http://svc/",
    "gatus.web.conditions.0": "[STATUS] == 200",
}


class FakeContainer:
    def __init__(self, name, status="running", labels=None, restart_count=0):
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
            "RestartCount": restart_count,
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


class RaisingClient:
    class _C:
        def list(self, all=False, ignore_removed=False):
            raise RuntimeError("daemon down")

    @property
    def containers(self):
        return RaisingClient._C()


def _monitor(client, store=None):
    return ContainerMonitor(
        client=client,
        pusher=None,
        token="t",
        stack_map={},
        thresholds=Thresholds(),
        excludes=[],
        store=store,
    )


# --- Fix 1: a docker listing failure must never write an empty config ---


def test_gather_endpoints_raises_on_list_failure():
    with pytest.raises(DockerListError):
        gather_endpoints(RaisingClient())


def test_monitor_reconcile_raises_on_list_failure():
    with pytest.raises(DockerListError):
        _monitor(RaisingClient()).reconcile()


def test_tick_keeps_last_config_when_docker_fails(tmp_path):
    out = tmp_path / "out.yaml"
    writer = Writer(str(out))
    good = FakeContainer("svc", labels=TIER1)

    # A healthy tick writes the real config.
    agmain._tick(FakeClient([good]), writer, None)
    first = out.read_text()
    assert "web" in first

    # The daemon then fails to list. The cycle must be skipped, not clobbered.
    agmain._tick(RaisingClient(), writer, None)
    assert out.read_text() == first


# --- Fix 2: monitor state survives a reconnect (client swap only) ---


def test_monitor_state_survives_client_swap():
    c = FakeContainer("svc", status="running")
    m = _monitor(FakeClient([c]))
    m.reconcile()
    assert "svc" in m._seen_running

    # Simulate a reconnect the way run() does: swap only the client, keep the
    # monitor. A now-stopped container must stay monitored, not be reclassified.
    stopped = FakeContainer("svc", status="exited")
    m.client = FakeClient([stopped])
    _, verdicts = m.reconcile()
    keys = [k for k, _ in verdicts]
    assert any("svc" in k for k in keys)
    assert verdicts[0][1].success is False


# --- Fix 4: sub-second resync interval floors at 1s ---


def test_resync_interval_floors_at_one():
    assert max(1, int(parse_duration_seconds("500ms", 15))) == 1
    assert max(1, int(parse_duration_seconds("20s", 15))) == 20
    assert agmain.INTERVAL >= 1


# --- Fix 5: durable atomic write stays correct ---


def test_atomic_write_correct_and_dir_fsync_safe(tmp_path):
    p = tmp_path / "sub" / "f.yaml"
    _atomic_write(str(p), "hello: world\n")
    assert p.read_text() == "hello: world\n"
    _fsync_dir(str(tmp_path))  # must not raise


# --- Fix 6: tier-1 endpoints kept for stopped-but-present, dropped on removal ---


def test_tier1_keeps_stopped_container_drops_removed():
    stopped = FakeContainer("svc", status="exited", labels=TIER1)
    eps = gather_endpoints(FakeClient([stopped]))
    assert [e["name"] for e in eps] == ["web"]  # still declared while it exists

    eps_gone = gather_endpoints(FakeClient([]))
    assert eps_gone == []  # removed -> dropped
