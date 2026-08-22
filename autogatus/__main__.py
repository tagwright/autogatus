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
import threading
import time

import docker

from .alerts import parse_type_list, resolve_allowlist
from .health import Thresholds
from .logging_setup import register_secret, setup_logging
from .monitor import ContainerMonitor
from .push import GatusPusher
from .reconcile import Writer, gather_endpoints
from .stackmap import load_stack_map
from .store import Store

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
LOG_FORMAT = os.environ.get("AUTOGATUS_LOG_FORMAT", "text").lower()
ACCESS_LOG_LEVEL = os.environ.get("AUTOGATUS_ACCESS_LOG_LEVEL", "INFO").upper()

MONITOR = _bool("AUTOGATUS_MONITOR_CONTAINERS", False)
GATUS_URL = os.environ.get("AUTOGATUS_GATUS_URL", "http://gatus:8080")
PUSH_TOKEN = os.environ.get("AUTOGATUS_PUSH_TOKEN", "").strip()
STACK_MAP_PATH = os.environ.get("AUTOGATUS_STACK_MAP", "")
HEADLINE_METRIC = os.environ.get("AUTOGATUS_HEADLINE_METRIC", "mem_used_mb")
HEARTBEAT_INTERVAL = os.environ.get("AUTOGATUS_HEARTBEAT_INTERVAL", "90s")
WEB = _bool("AUTOGATUS_WEB", True)
WEB_PORT = int(os.environ.get("AUTOGATUS_WEB_PORT", "8080"))
EXCLUDES = [x.strip() for x in os.environ.get(
    "AUTOGATUS_EXCLUDE", "autogatus,claude-code").split(",") if x.strip()]
MEM_THRESHOLD = _float_or_none("AUTOGATUS_MEM_THRESHOLD") if "AUTOGATUS_MEM_THRESHOLD" in os.environ else 95.0
CPU_THRESHOLD = _float_or_none("AUTOGATUS_CPU_THRESHOLD")

# Alert routing: which Gatus providers each endpoint may fire. The allowlist is
# derived from Gatus's own configured providers when AUTOGATUS_GATUS_CONFIG points
# at its config; ALERT_TYPES is both the Tier-2 default channels and the fallback
# allowlist when no config is mounted.
GATUS_CONFIG_PATH = os.environ.get("AUTOGATUS_GATUS_CONFIG", "")
ALERT_TYPES = parse_type_list(os.environ.get("AUTOGATUS_ALERT_TYPES")) or []
OUTPUT_BASENAME = os.path.basename(OUTPUT_PATH)

_running = True
_last_allowlist = None


def _handle_signal(signum, _frame):
    global _running
    logger.info("received signal %s, shutting down", signum)
    _running = False


def _start_web(store) -> None:
    from waitress import serve

    from .web import init
    app = init(store, access_log_level=ACCESS_LOG_LEVEL)

    def _serve():
        logger.info("serving container detail view on :%s (/details)", WEB_PORT)
        serve(app, host="0.0.0.0", port=WEB_PORT, _quiet=True)

    threading.Thread(target=_serve, daemon=True, name="web").start()


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
    setup_logging(level=LOG_LEVEL, fmt=LOG_FORMAT)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "autogatus starting: output=%s interval=%ss monitor_containers=%s",
        OUTPUT_PATH, INTERVAL, MONITOR,
    )

    writer = Writer(OUTPUT_PATH)

    store = None
    monitor = None
    if MONITOR:
        store = Store()
        token = _acquire_token()
        register_secret(token)
        if WEB:
            _start_web(store)
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
                store=store,
                default_alert_types=ALERT_TYPES or ["custom"],
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
    global _last_allowlist
    # Re-derive each cycle so adding a provider to Gatus self-heals with no
    # restart. Log only when the resolved set changes.
    allowlist, source = resolve_allowlist(GATUS_CONFIG_PATH, ALERT_TYPES, skip_basename=OUTPUT_BASENAME)
    if allowlist != _last_allowlist:
        logger.info("alert allowlist (%s): %s", source, ", ".join(sorted(allowlist)) or "(none)")
        _last_allowlist = allowlist

    try:
        endpoints = gather_endpoints(client, default_group=DEFAULT_GROUP, allowlist=allowlist)
    except Exception as e:
        logger.error("label discovery failed: %s", e)
        endpoints = []

    declarations, verdicts = ([], [])
    if monitor is not None:
        declarations, verdicts = monitor.reconcile(allowlist=allowlist)

    # Write declarations first so Gatus loads them before we push their statuses.
    wrote = writer.reconcile(endpoints, external_endpoints=declarations)

    if monitor is not None:
        pushed = monitor.push_all(verdicts)
        total = len(verdicts)
        failed = total - pushed
        # Keep a healthy INFO run quiet: only surface a summary when the config
        # changed or a push failed. Steady-state cycles stay at DEBUG.
        if wrote or failed:
            logger.info(
                "reconcile: %d monitored, %d pushed, %d failed%s",
                total, pushed, failed, ", config written" if wrote else "",
            )
        else:
            logger.debug("reconcile: %d monitored, %d pushed (no change)", total, pushed)


if __name__ == "__main__":
    sys.exit(run())
