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
from .collector import collect_health
from .health import Thresholds, evaluate

logger = logging.getLogger("autogatus")

# Per-container override of alert channels (distinct from the gatus.* opt-in
# namespace used to declare probe endpoints).
ALERTS_LABEL = "autogatus.alerts"

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
        self._seen_running = set()
        self._prev_restart = {}

    def _excluded(self, name: str) -> bool:
        return any(pat and pat in name for pat in self.excludes)

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

        # Eligibility is cheap (labels/status); do it sequentially.
        worklist = []
        for c in containers:
            if self._excluded(c.name):
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

        ts = time.time()
        declarations = []
        verdicts = []
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
            alerts = filter_alerts(requested, allowlist, key) if allowlist is not None else requested
            if alerts:
                decl["alerts"] = alerts
            declarations.append(decl)
            verdicts.append((key, verdict))
            if self.store is not None:
                self.store.update(key, stack, name, health, verdict, ts)

        if self.store is not None:
            self.store.prune({k for k, _ in verdicts})

        # Forget containers that no longer exist at all.
        self._seen_running &= present
        self._prev_restart = {k: v for k, v in self._prev_restart.items() if k in present}
        return declarations, verdicts

    def push_all(self, verdicts) -> int:
        pushed = 0
        for key, v in verdicts:
            if self.pusher.push(key, v.success, v.error, v.headline):
                pushed += 1
        return pushed
