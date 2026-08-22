"""autogatus entrypoint.

Two subsystems, both writing into one generated Gatus config file that Gatus
hot-reloads:

  1. Label discovery (always on): containers with gatus.* labels become normal
     Gatus endpoints (Gatus probes them).
  2. Container monitoring (opt-in via AUTOGATUS_MONITOR_CONTAINERS): every
     container becomes a pushed external endpoint carrying liveness + health +
     resource thresholds, grouped by stack. autogatus does the evaluation and
     pushes the verdict; Gatus is the dashboard.
"""

from __future__ import annotations

import logging
import os
import secrets
import signal
import sys
import time

import docker

from .health import Thresholds
from .monitor import ContainerMonitor
from .push import GatusPusher
from .reconcile import Writer, gather_endpoints
from .stackmap import load_stack_map

logger = logging.getLogger("autogatus")


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("true", "1", "yes", "on")


def _float_or_none(name: str):
    v = os.environ.get(name, "").strip()
    if not v or v.lower() in ("none", "off", "disabled"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


OUTPUT_PATH = os.environ.get("AUTOGATUS_OUTPUT", "/output/autogatus.yaml")
DEFAULT_GROUP = os.environ.get("AUTOGATUS_DEFAULT_GROUP", "")
INTERVAL = int(os.environ.get("AUTOGATUS_RESYNC_INTERVAL", "15"))
LOG_LEVEL = os.environ.get("AUTOGATUS_LOG_LEVEL", "INFO").upper()

MONITOR = _bool("AUTOGATUS_MONITOR_CONTAINERS", False)
GATUS_URL = os.environ.get("AUTOGATUS_GATUS_URL", "http://gatus:8080")
PUSH_TOKEN = os.environ.get("AUTOGATUS_PUSH_TOKEN", "").strip()
STACK_MAP_PATH = os.environ.get("AUTOGATUS_STACK_MAP", "")
HEADLINE_METRIC = os.environ.get("AUTOGATUS_HEADLINE_METRIC", "mem_used_mb")
HEARTBEAT_INTERVAL = os.environ.get("AUTOGATUS_HEARTBEAT_INTERVAL", "90s")
EXCLUDES = [x.strip() for x in os.environ.get(
    "AUTOGATUS_EXCLUDE", "autogatus,claude-code").split(",") if x.strip()]
MEM_THRESHOLD = _float_or_none("AUTOGATUS_MEM_THRESHOLD") if "AUTOGATUS_MEM_THRESHOLD" in os.environ else 95.0
CPU_THRESHOLD = _float_or_none("AUTOGATUS_CPU_THRESHOLD")

_running = True


def _handle_signal(signum, _frame):
    global _running
    logger.info("received signal %s, shutting down", signum)
    _running = False


def _acquire_token() -> str:
    """Stable push token. Explicit env wins; otherwise persist a generated token
    next to the output so it survives restarts and Gatus never sees a token
    change (which would 401 pushes until it reloaded)."""
    if PUSH_TOKEN:
        return PUSH_TOKEN
    token_path = os.path.join(os.path.dirname(OUTPUT_PATH) or ".", ".autogatus-token")
    try:
        with open(token_path) as f:
            tok = f.read().strip()
        if tok:
            logger.info("reusing persisted push token from %s", token_path)
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
        with open(token_path, "w") as f:
            f.write(tok)
        logger.info("generated and persisted a new push token to %s", token_path)
    except Exception as e:
        logger.warning("could not persist token (%s); using an in-memory one", e)
    return tok


def run() -> int:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "autogatus starting: output=%s interval=%ss monitor_containers=%s",
        OUTPUT_PATH, INTERVAL, MONITOR,
    )

    writer = Writer(OUTPUT_PATH)

    monitor = None
    if MONITOR:
        token = _acquire_token()
        logger.info(
            "container monitoring on: gatus=%s thresholds(mem=%s,cpu=%s) headline=%s excludes=%s",
            GATUS_URL, MEM_THRESHOLD, CPU_THRESHOLD, HEADLINE_METRIC, EXCLUDES,
        )

    while _running:
        try:
            client = docker.from_env()
            client.ping()
            logger.info("connected to Docker daemon (engine %s)", client.version().get("Version", "?"))
        except Exception as e:
            logger.warning("cannot reach Docker (%s); retrying in %ss", e, INTERVAL)
            time.sleep(INTERVAL)
            continue

        if MONITOR:
            monitor = ContainerMonitor(
                client=client,
                pusher=GatusPusher(GATUS_URL, token),
                token=token,
                stack_map=load_stack_map(STACK_MAP_PATH),
                thresholds=Thresholds(mem_percent=MEM_THRESHOLD, cpu_percent=CPU_THRESHOLD),
                excludes=EXCLUDES,
                headline_metric=HEADLINE_METRIC,
                heartbeat_interval=HEARTBEAT_INTERVAL,
                default_group=DEFAULT_GROUP,
            )

        try:
            while _running:
                _tick(client, writer, monitor)
                time.sleep(INTERVAL)
        except Exception as e:
            logger.warning("loop error (%s); reconnecting", e)
            time.sleep(min(INTERVAL, 10))

    logger.info("autogatus stopped")
    return 0


def _tick(client, writer: Writer, monitor) -> None:
    try:
        endpoints = gather_endpoints(client, default_group=DEFAULT_GROUP)
    except Exception as e:
        logger.error("label discovery failed: %s", e)
        endpoints = []

    declarations, verdicts = ([], [])
    if monitor is not None:
        declarations, verdicts = monitor.reconcile()

    # Write declarations first so Gatus loads them before we push their statuses.
    writer.reconcile(endpoints, external_endpoints=declarations)

    if monitor is not None:
        pushed = monitor.push_all(verdicts)
        logger.debug("pushed %d/%d container statuses", pushed, len(verdicts))


if __name__ == "__main__":
    sys.exit(run())
