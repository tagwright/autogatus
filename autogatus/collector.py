"""Build ContainerHealth snapshots from the Docker API. Thin Docker glue."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .health import ContainerHealth
from .stats import block_io, cpu_percent, memory, network

logger = logging.getLogger("autogatus")


def _uptime_seconds(started_at: str):
    if not started_at or started_at.startswith("0001-01-01"):
        return None
    try:
        # Docker emits RFC3339 with nanoseconds; trim to microseconds for fromisoformat.
        s = started_at.replace("Z", "+00:00")
        if "." in s:
            head, tail = s.split(".", 1)
            frac = tail[:6]
            tz = ""
            for marker in ("+", "-"):
                if marker in tail[6:]:
                    tz = tail[6:][tail[6:].index(marker) :]
                    break
            if tail.endswith("+00:00"):
                tz = "+00:00"
            s = f"{head}.{frac}{tz or '+00:00'}"
        started = datetime.fromisoformat(s)
        return int((datetime.now(timezone.utc) - started).total_seconds())
    except Exception:
        return None


def collect_health(container, stack: str, with_stats: bool = True) -> ContainerHealth:
    """Snapshot one container. ``container`` is a docker SDK Container object."""
    attrs = container.attrs or {}
    state = attrs.get("State", {}) or {}
    status = state.get("Status", "unknown")

    health_status = None
    health_output = ""
    health = state.get("Health")
    if health:
        health_status = health.get("Status")
        log = health.get("Log") or []
        if log:
            health_output = (log[-1].get("Output") or "").strip()

    h = ContainerHealth(
        name=container.name,
        stack=stack,
        state=status,
        exit_code=state.get("ExitCode"),
        oom_killed=bool(state.get("OOMKilled")),
        restart_count=int(attrs.get("RestartCount", 0) or 0),
        health_status=health_status,
        health_output=health_output,
        uptime_seconds=_uptime_seconds(state.get("StartedAt", "")),
    )

    if with_stats and status == "running":
        try:
            s = container.stats(stream=False)
            h.cpu_percent = cpu_percent(s)
            mem = memory(s)
            if mem:
                h.mem_used, h.mem_limit, h.mem_percent = mem
            net = network(s)
            if net:
                h.net_rx, h.net_tx = net
            blk = block_io(s)
            if blk:
                h.blk_read, h.blk_write = blk
        except Exception as e:
            logger.debug("stats unavailable for %s: %s", container.name, e)

    return h
