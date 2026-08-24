"""Container monitoring: collect health for every container, declare external
endpoints in the Gatus config, and push per-container verdicts.

Behaviour that keeps the dashboard honest:
- A container is monitored once we've seen it running. That way a service that
  crashes or is stopped keeps its endpoint and flips to failing (with a
  heartbeat backstop), while a one-shot that merely exited before we ever saw it
  run is not monitored (no phantom "down" rows).
- If a container is removed entirely, its declaration is dropped.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .alerts import build_alerts, filter_alerts, parse_type_list
from .checks import parse_container_checks, parse_duration_seconds, run_check
from .collector import collect_health
from .health import Thresholds, evaluate

logger = logging.getLogger("autogatus")

# Per-container override of alert channels (distinct from the gatus.* opt-in
# namespace used to declare probe endpoints).
ALERTS_LABEL = "autogatus.alerts"
# Per-container opt-out of tier-2 auto monitoring. Note the polarity: gatus.enable
# opts a container INTO tier-1 probed endpoints, autogatus.enable=false opts it OUT
# of tier-2 auto monitoring. Declared autogatus.check.* checks still run regardless.
ENABLE_LABEL = "autogatus.enable"
_FALSE = {"false", "0", "no", "off"}

_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")
# Gatus builds an endpoint key by sanitizing group and name (underscores and
# other non [a-zA-Z0-9-] chars become hyphens) and joining with "_". We must
# compute the push key identically or the push 404s.
_KEYSAFE = re.compile(r"[^a-zA-Z0-9-]+")


def _safe(s: str) -> str:
    return _SAFE.sub("-", s).strip("-") or "unknown"


def _gatus_key(group: str, name: str) -> str:
    return f"{_KEYSAFE.sub('-', group)}_{_KEYSAFE.sub('-', name)}"


class ContainerMonitor:
    def __init__(
        self,
        client,
        pusher,
        token: str,
        stack_map: dict,
        thresholds: Thresholds,
        excludes,
        headline_metric: str = "mem_percent",
        heartbeat_interval: str = "90s",
        default_group: str = "",
        max_workers: int = 16,
        store=None,
        default_alert_types=None,
        exec_enabled=False,
        resync_interval: int = 15,
    ):
        self.client = client
        self.pusher = pusher
        self.token = token
        self.stack_map = stack_map or {}
        self.thresholds = thresholds
        self.excludes = list(excludes or [])
        self.headline_metric = headline_metric
        self.heartbeat_interval = heartbeat_interval
        self.default_group = default_group
        self.max_workers = max_workers
        self.store = store
        # Default alert channels for auto-monitored containers, overridable per
        # container via the autogatus.alerts label.
        self.default_alert_types = list(default_alert_types) if default_alert_types else ["custom"]
        self.exec_enabled = exec_enabled
        self.resync_interval = resync_interval
        self._warned_exec_disabled = False
        self._seen_running: set[str] = set()
        self._prev_restart: dict[str, int] = {}
        # Checks run on their own cadence, so remember when each last ran and its
        # last verdict (reused between runs so the endpoint stays in the store).
        self._check_last_run: dict[str, float] = {}
        self._check_last_verdict: dict = {}

    def _excluded(self, name: str) -> bool:
        return any(pat and pat in name for pat in self.excludes)

    def _monitor_disabled(self, container) -> bool:
        """True if the container opted out of tier-2 monitoring with
        autogatus.enable=false. Checks are unaffected, they are a separate opt-in."""
        val = (container.labels or {}).get(ENABLE_LABEL)
        return val is not None and str(val).strip().lower() in _FALSE

    def _stack_for(self, container) -> str:
        service = (container.labels or {}).get("com.docker.compose.service", "")
        if service and service in self.stack_map:
            return _safe(self.stack_map[service])
        if self.default_group:
            return _safe(self.default_group)
        return _safe(container.name)

    def _alert_types_for(self, container) -> list:
        """Resolve a container's requested alert channels: its autogatus.alerts
        label if present, else the global default."""
        label = (container.labels or {}).get(ALERTS_LABEL)
        requested = parse_type_list(label)
        return requested if requested is not None else list(self.default_alert_types)

    def reconcile(self, allowlist=None):
        """Return ``(declarations, verdicts)``.

        ``declarations`` are Gatus external-endpoint dicts; ``verdicts`` is a list
        of ``(key, Verdict)`` to push after Gatus has loaded the declarations.
        ``allowlist`` is the set of providers Gatus has configured; requested
        alert channels not in it are dropped with a warning.
        """
        try:
            containers = self.client.containers.list(all=True)
        except Exception as e:
            logger.warning("could not list containers: %s", e)
            return [], []

        present = {c.name for c in containers}
        now = time.time()
        declarations = []
        push_verdicts = []
        declared_keys: set[str] = set()

        # --- Liveness: every non-excluded container that has not opted out ---
        # Eligibility is cheap (labels/status); do it sequentially.
        worklist = []
        for c in containers:
            if self._excluded(c.name) or self._monitor_disabled(c):
                continue
            running = c.status == "running"
            if running:
                self._seen_running.add(c.name)
            elif c.name not in self._seen_running:
                continue  # never observed running -> one-shot, skip
            worklist.append((c, running, self._stack_for(c)))

        # Stats collection is the slow part (one daemon call per container), so
        # fan it out across a thread pool. Each call is an independent request.
        def _collect(item):
            c, running, stack = item
            return c, stack, collect_health(c, stack, with_stats=running)

        healths = []
        if worklist:
            workers = min(self.max_workers, len(worklist))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                healths = list(pool.map(_collect, worklist))

        for c, stack, health in healths:
            verdict = evaluate(
                health,
                self.thresholds,
                prev_restart_count=self._prev_restart.get(c.name),
                headline_metric=self.headline_metric,
            )
            self._prev_restart[c.name] = health.restart_count
            name = c.name
            key = _gatus_key(stack, name)
            decl = {
                "name": name,
                "group": stack,
                "token": self.token,
                "heartbeat": {"interval": self.heartbeat_interval},
            }
            requested = build_alerts(self._alert_types_for(c), f"{name} ({stack})")
            alerts = (
                filter_alerts(requested, allowlist, key) if allowlist is not None else requested
            )
            if alerts:
                decl["alerts"] = alerts
            declarations.append(decl)
            declared_keys.add(key)
            push_verdicts.append((key, verdict))
            if self.store is not None:
                self.store.update(key, stack, name, health, verdict, now)

        # --- Checks: exec/tcp probes autogatus runs, on their own cadence. These
        # are additional to liveness and apply to any container carrying the
        # labels, even one excluded or opted out of liveness monitoring. A check's
        # alert channels cascade: its own .alerts, else the container's
        # autogatus.alerts, else the global default. ---
        default_check_interval = f"{self.resync_interval}s"
        check_items = []
        saw_disabled_exec = False
        for c in containers:
            checks, disabled = parse_container_checks(
                c.labels or {},
                c.name,
                self._stack_for(c),
                default_alert_types=self._alert_types_for(c),
                default_interval=default_check_interval,
                exec_enabled=self.exec_enabled,
            )
            saw_disabled_exec = saw_disabled_exec or disabled
            for chk in checks:
                check_items.append((chk, c))

        if saw_disabled_exec and not self._warned_exec_disabled:
            logger.warning(
                "exec checks present but AUTOGATUS_ENABLE_EXEC is off; ignoring them "
                "(exec runs commands in containers, so it is opt-in)"
            )
            self._warned_exec_disabled = True

        # Decide each check's key, run cadence, and derived heartbeat. The
        # heartbeat is a multiple of the run interval, so one skipped cycle does
        # not false-alarm but a stopped check still goes down.
        meta = []
        for chk, c in check_items:
            key = _gatus_key(chk.group, chk.name)
            run_secs = parse_duration_seconds(chk.interval, float(self.resync_interval))
            heartbeat = f"{max(int(run_secs * 3), 30)}s"
            due = (now - self._check_last_run.get(key, 0.0)) >= run_secs
            meta.append((chk, c, key, heartbeat, due))

        # Run only the checks that are due this cycle, concurrently.
        to_run = [(chk, c, key) for (chk, c, key, _hb, due) in meta if due]
        if to_run:
            workers = min(self.max_workers, len(to_run))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(lambda it: (it[2], run_check(it[0], it[1])), to_run))
            for key, verdict in results:
                self._check_last_run[key] = now
                self._check_last_verdict[key] = verdict

        # Declare every check; push only the ones that ran this cycle. Between
        # runs the last verdict keeps the endpoint alive in the store.
        for chk, _c, key, heartbeat, due in meta:
            decl = {
                "name": chk.name,
                "group": chk.group,
                "token": self.token,
                "heartbeat": {"interval": heartbeat},
            }
            requested = build_alerts(chk.alert_types or [], chk.description)
            alerts = (
                filter_alerts(requested, allowlist, key) if allowlist is not None else requested
            )
            if alerts:
                decl["alerts"] = alerts
            declarations.append(decl)
            declared_keys.add(key)
            verdict = self._check_last_verdict.get(key)
            if due and verdict is not None:
                push_verdicts.append((key, verdict))
            if self.store is not None and verdict is not None:
                self.store.update(key, chk.group, chk.name, None, verdict, now)

        if self.store is not None:
            self.store.prune(declared_keys)

        # Forget containers and checks that no longer exist.
        self._seen_running &= present
        self._prev_restart = {k: v for k, v in self._prev_restart.items() if k in present}
        self._check_last_run = {k: v for k, v in self._check_last_run.items() if k in declared_keys}
        self._check_last_verdict = {
            k: v for k, v in self._check_last_verdict.items() if k in declared_keys
        }
        return declarations, push_verdicts

    def push_all(self, verdicts) -> int:
        pushed = 0
        for key, v in verdicts:
            if self.pusher.push(key, v.success, v.error, v.headline):
                pushed += 1
        return pushed
