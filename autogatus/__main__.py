"""autogatus entrypoint.

Watches the Docker daemon for containers carrying ``gatus.*`` labels, compiles
them into a Gatus endpoint file, and writes it into the Gatus config directory.
Gatus hot-reloads the change. This is the Traefik-shaped, config-as-code sidecar
for Gatus: your monitoring definition lives on the containers themselves.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

import docker

from .reconcile import Writer, gather_endpoints

logger = logging.getLogger("autogatus")

OUTPUT_PATH = os.environ.get("AUTOGATUS_OUTPUT", "/output/autogatus.yaml")
DEFAULT_GROUP = os.environ.get("AUTOGATUS_DEFAULT_GROUP", "")
RESYNC_INTERVAL = int(os.environ.get("AUTOGATUS_RESYNC_INTERVAL", "15"))
LOG_LEVEL = os.environ.get("AUTOGATUS_LOG_LEVEL", "INFO").upper()

# Docker events that can change the set of running, labelled containers.
_WATCH_EVENTS = {"start", "die", "stop", "destroy", "health_status"}

_running = True


def _handle_signal(signum, _frame):
    global _running
    logger.info("received signal %s, shutting down", signum)
    _running = False


def _connect() -> docker.DockerClient:
    client = docker.from_env()
    client.ping()
    version = client.version().get("Version", "?")
    logger.info("connected to Docker daemon (engine %s)", version)
    return client


def run() -> int:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "autogatus starting: output=%s resync=%ss default_group=%r",
        OUTPUT_PATH, RESYNC_INTERVAL, DEFAULT_GROUP or "(container name)",
    )

    writer = Writer(OUTPUT_PATH)

    while _running:
        try:
            client = _connect()
        except Exception as e:  # daemon not up yet / socket missing
            logger.warning("cannot reach Docker (%s); retrying in %ss", e, RESYNC_INTERVAL)
            time.sleep(RESYNC_INTERVAL)
            continue

        # Reconcile immediately, then react to events with a periodic resync as
        # a backstop. events() blocks up to the timeout, so the loop also wakes
        # every RESYNC_INTERVAL even when the daemon is quiet.
        try:
            _reconcile(client, writer)
            while _running:
                since = time.time()
                got_event = False
                for event in client.events(
                    decode=True,
                    filters={"type": "container"},
                    since=int(since),
                    until=int(since + RESYNC_INTERVAL),
                ):
                    if event.get("Action", "").split(":")[0] in _WATCH_EVENTS:
                        got_event = True
                # Always resync on the interval; also resync promptly on events.
                if got_event:
                    logger.debug("container event(s) observed, reconciling")
                _reconcile(client, writer)
        except Exception as e:
            logger.warning("event loop error (%s); reconnecting", e)
            time.sleep(min(RESYNC_INTERVAL, 10))

    logger.info("autogatus stopped")
    return 0


def _reconcile(client, writer: Writer) -> None:
    try:
        endpoints = gather_endpoints(client, default_group=DEFAULT_GROUP)
        writer.reconcile(endpoints)
    except Exception as e:
        logger.error("reconcile failed: %s", e)


if __name__ == "__main__":
    sys.exit(run())
