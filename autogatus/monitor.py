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

from .alerts import build_alerts, filter_alerts, parse_alerts
from .checks import parse_container_checks, run_check
from .collector import _uptime_seconds, collect_health
from .duration import parse_duration_seconds
from .health import Thresholds, Verdict, evaluate
from .reconcile import DockerListError

logger = logging.getLogger("autogatus")

# Per-container override of alert channels (distinct from the gatus.* opt-in
# namespace used to declare probe endpoints).
ALERTS_LABEL = "autogatus.alerts"
# Per-container opt-out of tier-2 auto monitoring. Note the polarity: gatus.enable
# opts a container INTO tier-1 probed endpoints, autogatus.enable=false opts it OUT
# of tier-2 auto monitoring. Declared autogatus.check.* checks still run regardless.
ENABLE_LABEL = "autogatus.enable"
_FALSE = {"false", "0", "no", "off"}
# Per-container threshold overrides for tier-2 evaluation (percent, none disables).
MEM_THRESHOLD_LABEL = "autogatus.mem-threshold"
CPU_THRESHOLD_LABEL = "autogatus.cpu-threshold"
# Startup grace: suppress a container's alerts while its uptime is under this.
SNOOZE_LABEL = "autogatus.snooze"

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
        failure_latch: int = 3,
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
        self.failure_latch = failure_latch
        self._warned_exec_disabled = False
        # Point-in-time failures (OOM, a restart bump) show for one cycle then
        # recover, so Gatus's default failure-threshold of 3 never fires. Latch
        # them failing for a few cycles. name -> [remaining_cycles, reason].
        self._latched: dict[str, list] = {}
        self._warned_bad_threshold: set[str] = set()
        self._seen_running: set[str] = set()
        self._prev_restart: dict[str, int] = {}
        # Checks run on their own cadence, so remember when each last ran and its
        # last verdict (reused between runs so the endpoint stays in the store).
        self._check_last_run: dict[str, float] = {}
        self._check_last_verdict: dict = {}
        # A wedged exec check leaves its worker thread running. We track those by
        # key so we do not launch another run on top of it (which would pile up
        # orphan processes in the target container). Warnings are deduped.
        self._check_inflight: dict = {}
        self._warned_inflight: set[str] = set()
        # Deduped warnings for two distinct group/name inputs that sanitize to the
        # same Gatus key and would overwrite each other.
        self._warned_collisions: set[str] = set()

    def _excluded(self, name: str) -> bool:
        return any(pat and pat in name for pat in self.excludes)

    def _note_key(self, key_sources: dict, key: str, group: str, name: str) -> None:
        """Record a key's source and warn (once) if a different group/name already
        maps to the same sanitized key, since Gatus would silently overwrite one."""
        prior = key_sources.get(key)
        if prior is None:
            key_sources[key] = (group, name)
        elif prior != (group, name) and key not in self._warned_collisions:
            logger.warning(
                "key collision: %s and %s both map to %s, one overwrites the other in Gatus",
                prior,
                (group, name),
                key,
            )
            self._warned_collisions.add(key)

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

    def _threshold_label(self, labels, key, default):
        if key not in labels:
            return default
        raw = str(labels[key]).strip().lower()
        if raw in {"", "none", "off", "disabled"}:
            return None
        try:
            return float(raw)
        except ValueError:
            if key not in self._warned_bad_threshold:
                logger.warning("bad %s=%r, using the global default", key, labels[key])
                self._warned_bad_threshold.add(key)
            return default

    def _thresholds_for(self, container):
        """The container's Thresholds, with autogatus.mem-threshold /
        autogatus.cpu-threshold overriding the globals when present."""
        labels = container.labels or {}
        mem = self._threshold_label(labels, MEM_THRESHOLD_LABEL, self.thresholds.mem_percent)
        cpu = self._threshold_label(labels, CPU_THRESHOLD_LABEL, self.thresholds.cpu_percent)
        if mem == self.thresholds.mem_percent and cpu == self.thresholds.cpu_percent:
            return self.thresholds
        return Thresholds(
            mem_percent=mem,
            cpu_percent=cpu,
            fail_on_restart=self.thresholds.fail_on_restart,
            fail_on_unhealthy=self.thresholds.fail_on_unhealthy,
        )

    def _snoozed(self, container) -> bool:
        """True while a container's uptime is under its autogatus.snooze window,
        so a deploy or restart does not page during the settling period."""
        val = (container.labels or {}).get(SNOOZE_LABEL)
        if not val or str(val).strip().lower() in {"", "none", "0"}:
            return False
        window = parse_duration_seconds(val, 0.0)
        if window <= 0:
            return False
        state = (getattr(container, "attrs", None) or {}).get("State", {}) or {}
        up = _uptime_seconds(state.get("StartedAt", ""))
        return up is not None and up < window

    def _apply_latch(self, name, health, prev_restart, verdict):
        """Arm a latch on a point-in-time event (OOM or a restart bump) and, while
        a latch is active, force the verdict failing so Gatus's threshold can fire."""
        restarted = prev_restart is not None and health.restart_count > prev_restart
        oom = health.state == "running" and health.oom_killed
        if (restarted or oom) and self.failure_latch > 0:
            reason = "OOMKilled" if oom else f"restarted ({prev_restart}->{health.restart_count})"
            self._latched[name] = [self.failure_latch, reason]
        latch = self._latched.get(name)
        if not latch:
            return verdict
        latch[0] -= 1
        if latch[0] <= 0:
            self._latched.pop(name, None)
        if verdict.success:
            reason = latch[1]
            return Verdict(
                False, f"{reason} (latched) | {verdict.error}", verdict.headline, [reason]
            )
        return verdict

    @staticmethod
    def _container_alert_fields(labels: dict) -> dict:
        """Pull the autogatus.alerts config (scalar and indexed) into a field
        dict parse_alerts understands, keyed without the autogatus. prefix."""
        out = {}
        for k, v in (labels or {}).items():
            if k == ALERTS_LABEL or k.startswith(ALERTS_LABEL + "."):
                out[k[len("autogatus.") :]] = v
        return out

    def _alerts_for(self, container) -> list:
        """Resolve a container's requested alerts as full Gatus alert dicts: its
        autogatus.alerts (shorthand or structured) if present, else the global
        default expanded from AUTOGATUS_ALERT_TYPES."""
        name = container.name
        stack = self._stack_for(container)
        fields = self._container_alert_fields(container.labels or {})
        alerts = parse_alerts(
            fields, default_description=f"{name} ({stack})", context=f"container {name}"
        )
        if alerts is None:
            alerts = build_alerts(self.default_alert_types, f"{name} ({stack})")
        return alerts

    def reconcile(self, allowlist=None):
        """Return ``(declarations, verdicts)``.

        ``declarations`` are Gatus external-endpoint dicts; ``verdicts`` is a list
        of ``(key, Verdict)`` to push after Gatus has loaded the declarations.
        ``allowlist`` is the set of providers Gatus has configured; requested
        alert channels not in it are dropped with a warning. Raises
        ``DockerListError`` if the daemon cannot be listed, so the caller keeps
        the last-good config instead of writing an empty one.
        """
        try:
            containers = self.client.containers.list(all=True, ignore_removed=True)
        except Exception as e:
            raise DockerListError(str(e)) from e

        present = {c.name for c in containers}
        now = time.time()
        declarations = []
        push_verdicts = []
        declared_keys: set[str] = set()
        key_sources: dict[str, tuple] = {}

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
            prev_restart = self._prev_restart.get(c.name)
            verdict = evaluate(
                health,
                self._thresholds_for(c),
                prev_restart_count=prev_restart,
                headline_metric=self.headline_metric,
            )
            verdict = self._apply_latch(c.name, health, prev_restart, verdict)
            self._prev_restart[c.name] = health.restart_count
            name = c.name
            key = _gatus_key(stack, name)
            decl = {
                "name": name,
                "group": stack,
                "token": self.token,
                "heartbeat": {"interval": self.heartbeat_interval},
            }
            # While snoozed, still push status but leave off the alerts block.
            requested = [] if self._snoozed(c) else self._alerts_for(c)
            alerts = (
                filter_alerts(requested, allowlist, key) if allowlist is not None else requested
            )
            if alerts:
                decl["alerts"] = alerts
            declarations.append(decl)
            declared_keys.add(key)
            self._note_key(key_sources, key, stack, name)
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
                default_alerts=self._alerts_for(c),
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

        # Run the due checks concurrently, but skip any whose previous run is
        # still wedged (its exec thread has not returned), so a hung command does
        # not stack orphan processes in the target container.
        to_run = []
        for chk, c, key, _hb, due in meta:
            if not due:
                continue
            prev = self._check_inflight.get(key)
            if prev is not None and prev.is_alive():
                if key not in self._warned_inflight:
                    logger.warning(
                        "check %s is still running from a previous cycle, skipping this run", key
                    )
                    self._warned_inflight.add(key)
                continue
            self._warned_inflight.discard(key)
            to_run.append((chk, c, key))
        if to_run:
            workers = min(self.max_workers, len(to_run))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(
                    pool.map(
                        lambda it: (
                            it[2],
                            run_check(it[0], it[1], registry=self._check_inflight, reg_key=it[2]),
                        ),
                        to_run,
                    )
                )
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
            requested = [] if self._snoozed(_c) else (chk.alerts or [])
            alerts = (
                filter_alerts(requested, allowlist, key) if allowlist is not None else requested
            )
            if alerts:
                decl["alerts"] = alerts
            declarations.append(decl)
            declared_keys.add(key)
            self._note_key(key_sources, key, chk.group, chk.name)
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
        self._latched = {k: v for k, v in self._latched.items() if k in present}
        self._check_last_run = {k: v for k, v in self._check_last_run.items() if k in declared_keys}
        self._check_last_verdict = {
            k: v for k, v in self._check_last_verdict.items() if k in declared_keys
        }
        self._warned_inflight &= declared_keys
        self._warned_collisions &= declared_keys
        return declarations, push_verdicts

    def push_all(self, verdicts) -> int:
        pushed = 0
        for key, v in verdicts:
            if self.pusher.push(key, v.success, v.error, v.headline):
                pushed += 1
        return pushed
