"""Container health snapshot and its evaluation into a Gatus push verdict.

``ContainerHealth`` is a plain data holder built from ``docker inspect`` state
plus a stats sample. ``evaluate`` turns it into a ``Verdict`` (the success bool,
a human error string, and the one headline number Gatus will graph). Both are
pure and Docker-free so the policy is fully unit-testable, and the same snapshot
can later back a Prometheus ``/metrics`` exporter without a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def _bytes_h(n: Optional[float]) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}PB"


@dataclass
class ContainerHealth:
    name: str
    stack: str
    state: str                      # running | exited | restarting | created | paused | dead
    exit_code: Optional[int] = None
    oom_killed: bool = False
    restart_count: int = 0
    health_status: Optional[str] = None       # healthy | unhealthy | starting | None
    health_output: str = ""                   # last healthcheck log line, if any
    cpu_percent: Optional[float] = None
    mem_used: Optional[float] = None
    mem_limit: Optional[float] = None
    mem_percent: Optional[float] = None
    uptime_seconds: Optional[int] = None
    net_rx: Optional[float] = None
    net_tx: Optional[float] = None
    blk_read: Optional[float] = None
    blk_write: Optional[float] = None

    @property
    def mem_used_mb(self) -> Optional[float]:
        return round(self.mem_used / (1024 * 1024), 1) if self.mem_used is not None else None


@dataclass
class Thresholds:
    # None disables a check. Defaults: fail on memory pressure and crashloops
    # (real "this will fall over" signals); CPU is reported but not a failure by
    # default, since a busy container is not a broken one.
    mem_percent: Optional[float] = 95.0
    cpu_percent: Optional[float] = None
    fail_on_restart: bool = True    # restart_count increased since last cycle
    fail_on_unhealthy: bool = True


@dataclass
class Verdict:
    success: bool
    error: str
    headline: Optional[float]       # the single number pushed as Gatus "duration"
    reasons: list = field(default_factory=list)


def evaluate(
    h: ContainerHealth,
    thresholds: Thresholds,
    prev_restart_count: Optional[int] = None,
    headline_metric: str = "mem_percent",
) -> Verdict:
    reasons = []

    if h.state != "running":
        detail = h.state
        if h.state == "exited" and h.exit_code is not None:
            detail = f"exited ({h.exit_code})"
        reasons.append(detail)
    else:
        if h.oom_killed:
            reasons.append("OOMKilled")
        if thresholds.fail_on_unhealthy and h.health_status == "unhealthy":
            out = h.health_output.strip().replace("\n", " ")[:120]
            reasons.append(f"unhealthy: {out}" if out else "unhealthy")
        if (
            thresholds.fail_on_restart
            and prev_restart_count is not None
            and h.restart_count > prev_restart_count
        ):
            reasons.append(f"restarted ({prev_restart_count}->{h.restart_count})")
        if thresholds.mem_percent is not None and h.mem_percent is not None:
            if h.mem_percent >= thresholds.mem_percent:
                reasons.append(f"mem {h.mem_percent}%>={thresholds.mem_percent}%")
        if thresholds.cpu_percent is not None and h.cpu_percent is not None:
            if h.cpu_percent >= thresholds.cpu_percent:
                reasons.append(f"cpu {h.cpu_percent}%>={thresholds.cpu_percent}%")

    # Status line always carries the live numbers so the failure text on the
    # Gatus card shows real stats, not just "down".
    status = (
        f"state={h.state} cpu={h.cpu_percent if h.cpu_percent is not None else '?'}% "
        f"mem={_bytes_h(h.mem_used)}/{_bytes_h(h.mem_limit)}"
        f"({h.mem_percent if h.mem_percent is not None else '?'}%) "
        f"restarts={h.restart_count} health={h.health_status or 'n/a'}"
    )

    headline = getattr(h, headline_metric, None)

    if reasons:
        return Verdict(False, f"{'; '.join(reasons)} | {status}", headline, reasons)
    return Verdict(True, status, headline, [])
