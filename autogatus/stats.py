"""Pure helpers to turn a Docker stats payload into CPU / memory numbers.

Kept free of any Docker dependency so the math is unit-testable from sample
payloads. Every function returns ``None`` when the required fields are absent
(a just-started or restarting container may report partial stats).
"""

from __future__ import annotations


def cpu_percent(stats: dict):
    """CPU usage as a percentage of one host core-second, summed over cores.

    Mirrors ``docker stats``: (cpu_delta / system_delta) * online_cpus * 100.
    A container pinning two full cores reads ~200.
    """
    try:
        cpu = stats["cpu_stats"]
        pre = stats["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        system_delta = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
    except (KeyError, TypeError):
        return None
    if system_delta <= 0 or cpu_delta < 0:
        return None
    online = cpu.get("online_cpus")
    if not online:
        percpu = cpu["cpu_usage"].get("percpu_usage") or []
        online = len(percpu) or 1
    return round((cpu_delta / system_delta) * online * 100.0, 1)


def memory(stats: dict):
    """Return ``(used_bytes, limit_bytes, percent)`` or ``None``.

    ``used`` subtracts reclaimable page cache the way ``docker stats`` does, so
    it reflects real working-set memory rather than cache. Percent is of the
    container's memory limit; a container with no limit reports the host total
    as its limit, so its percent is of host RAM (noted in the docs).
    """
    try:
        mem = stats["memory_stats"]
        usage = mem["usage"]
        limit = mem["limit"]
    except (KeyError, TypeError):
        return None
    detail = mem.get("stats", {}) or {}
    # cgroup v2 uses inactive_file; v1 uses cache. Prefer v2, fall back.
    reclaimable = detail.get("inactive_file", detail.get("cache", 0))
    used = usage - reclaimable
    if used < 0:
        used = usage
    if limit <= 0:
        return (used, limit, None)
    return (used, limit, round(used / limit * 100.0, 1))
