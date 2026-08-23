"""Cross-module gate-proofs: each asserts a guard/fail-closed path holds.

Companion gates proven in their own module files:
  alerts   - test_alerts.py::test_filter_alerts_drops_unconfigured / _none_disables
  checks   - test_checks.py::test_exec_gated_off_by_default / _malformed / _closed_port
  push     - test_push.py::test_push_{404,401,connection_exception}_*
  stackmap - test_stackmap.py::test_missing_file_falls_back / _garbage_*
  store    - test_store.py::test_prune_drops_absent_keys
  stats    - test_stats.py::test_*_missing / _no_limit_*
See tests/README.md for the full gate -> test map.
"""

from autogatus.health import Thresholds
from autogatus.monitor import ContainerMonitor
from autogatus.reconcile import gather_endpoints
from autogatus.store import Store


class FakeContainer:
    def __init__(self, name, status="running", labels=None, restart_count=0):
        self.name = name
        self.status = status
        self.labels = labels or {}
        self.attrs = {
            "State": {"Status": status, "ExitCode": 0, "OOMKilled": False,
                      "StartedAt": "0001-01-01T00:00:00Z"},
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

        def list(self, all=False):
            return self._outer._containers

    @property
    def containers(self):
        return FakeClient._C(self)


def _monitor(client, store=None):
    return ContainerMonitor(
        client=client, pusher=None, token="t", stack_map={},
        thresholds=Thresholds(), excludes=[], store=store,
    )


def test_monitor_skips_one_shot_exited_container():
    # Gate: a container never observed running (e.g. a completed one-shot) is
    # not monitored -> no phantom "down" row.
    client = FakeClient([FakeContainer("job", status="exited")])
    decls, verdicts = _monitor(client).reconcile()
    assert verdicts == []


def test_monitor_keeps_container_seen_then_exited():
    # Gate: once seen running, a later exit is reported (a real crash), not dropped.
    c = FakeContainer("svc", status="running")
    m = _monitor(client=FakeClient([c]))
    m.reconcile()                       # observed running
    c.status = "exited"
    c.attrs["State"]["Status"] = "exited"
    _, verdicts = m.reconcile()
    keys = [k for k, _ in verdicts]
    assert any("svc" in k for k in keys)
    assert verdicts[0][1].success is False


def test_monitor_prune_drops_removed_from_store():
    # Gate: a container gone from the daemon is pruned from the store.
    store = Store()
    c = FakeContainer("gone", status="running")
    client = FakeClient([c])
    m = _monitor(client, store=store)
    m.reconcile()
    assert store.all()                  # present
    client._containers = []             # container removed
    m.reconcile()
    assert store.all() == {}            # pruned


def test_gather_endpoints_skips_malformed_container():
    # Gate: one bad label set is skipped, the rest still reconcile (not fatal).
    good = FakeContainer("good", labels={"gatus.enable": "true", "gatus.web.url": "http://x/"})
    bad = FakeContainer("bad", labels={"gatus.enable": "true", "gatus.web.interval": "5s"})
    eps = gather_endpoints(FakeClient([bad, good]))
    names = [e["name"] for e in eps]
    assert names == ["web"]             # only the good one, no exception
