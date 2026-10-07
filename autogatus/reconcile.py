"""Gather endpoints from running Docker containers and write the Gatus file."""

from __future__ import annotations

import logging
import os
import tempfile

from .alerts import filter_alerts
from .labels import parse_container
from .offline import (
    BAD_VALUE_LOG,
    INVALID,
    LIVE,
    LIVE_LOG,
    NONE,
    OFFLINE_LABEL,
    PARKED,
    PARKED_LOG,
    RAN,
    RAN_LOG,
    classify,
    parse_offline,
)
from .render import render, render_stable

logger = logging.getLogger("autogatus")


class DockerListError(Exception):
    """The Docker daemon could not be listed this cycle.

    Raised instead of returning an empty result so the caller keeps the last-good
    generated config rather than writing an empty one (which would make Gatus drop
    every endpoint and its alerts during a transient daemon hiccup).
    """


def _container_id(container) -> str:
    return getattr(container, "id", None) or container.name


class OfflineNotes:
    """Logs what the ``autogatus.offline`` label is doing, once each.

    Parked is logged at INFO once per container id, live (drift) and ran at WARN
    once per container id, and an invalid value at WARN once per container name
    and value. A recreate gives the container a new id, so each new container is
    logged afresh. Every set is pruned to the containers that still exist.

    This runs on the tier-1 pass, which lists every container on every cycle
    whether or not container monitoring is on, so drift on a container with no
    liveness endpoint (excluded, opted out, or monitoring off) still shows up as
    its WARN line.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._parked: set[str] = set()
        self._live: set[str] = set()
        self._ran: set[str] = set()
        self._invalid: set[tuple[str, str]] = set()

    def observe(self, container) -> str:
        """Classify one container and log it if this is news. Returns the class."""
        labels = container.labels or {}
        raw = labels.get(OFFLINE_LABEL)
        if raw is None:
            return NONE
        name = container.name
        attrs = getattr(container, "attrs", None)
        oclass = classify(labels, attrs)
        if oclass == NONE and parse_offline(raw) == INVALID:
            tag = (name, str(raw))
            if tag not in self._invalid:
                logger.warning(BAD_VALUE_LOG, raw, name)
                self._invalid.add(tag)
            return NONE
        cid = _container_id(container)
        if oclass == PARKED and cid not in self._parked:
            logger.info(PARKED_LOG, name)
            self._parked.add(cid)
        elif oclass == LIVE and cid not in self._live:
            state = ((attrs or {}).get("State") or {}).get("Status")
            logger.warning(LIVE_LOG, name, state)
            self._live.add(cid)
        elif oclass == RAN and cid not in self._ran:
            logger.warning(RAN_LOG, name)
            self._ran.add(cid)
        return oclass

    def prune(self, containers) -> None:
        ids = {_container_id(c) for c in containers}
        names = {c.name for c in containers}
        self._parked &= ids
        self._live &= ids
        self._ran &= ids
        self._invalid = {t for t in self._invalid if t[0] in names}


# One per process, like the alert-drop warnings. A restart starts it empty.
offline_notes = OfflineNotes()


def gather_endpoints(client, default_group: str = "", allowlist=None) -> list:
    """Compile endpoints from every gatus-enabled container that still exists.

    Lists all containers, not just running ones, so a labeled service that
    crashes or is stopped keeps its endpoint and Gatus fails the probe and alerts.
    A container removed from the daemon drops out on its own. A single container
    with a malformed endpoint (e.g. missing url) is logged and skipped rather than
    taking the whole reconcile down. Requested alert channels are filtered against
    ``allowlist`` (the providers Gatus has configured); unconfigured ones are
    dropped with a warning. A parked container (``autogatus.offline``) keeps its
    endpoints, still probed and still enabled, with no alerts, so they page
    nobody. Raises ``DockerListError`` if the daemon cannot be listed, so the
    caller can keep the last config.
    """
    try:
        containers = client.containers.list(all=True, ignore_removed=True)
    except Exception as e:
        raise DockerListError(str(e)) from e
    endpoints = []
    for container in containers:
        labels = container.labels or {}
        name = container.name
        parked = offline_notes.observe(container) == PARKED
        try:
            found = parse_container(labels, container_name=name, default_group=default_group)
        except ValueError as e:
            logger.warning("skipping container %s: %s", name, e)
            continue
        for ep in found:
            if parked:
                ep.pop("alerts", None)
            elif allowlist is not None and "alerts" in ep:
                kept = filter_alerts(ep["alerts"], allowlist, f"{ep['group']}/{ep['name']}")
                if kept:
                    ep["alerts"] = kept
                else:
                    ep.pop("alerts")
        if found:
            logger.debug("container %s -> %d endpoint(s)", name, len(found))
            endpoints.extend(found)
    offline_notes.prune(containers)
    return endpoints


def _fsync_dir(directory: str) -> None:
    """fsync a directory so a rename inside it survives a power cut."""
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _atomic_write(path: str, content: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".autogatus-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # fsync the directory so the rename itself is durable, not just the data.
        # A hard power cut mid-cycle otherwise risks a truncated or empty file.
        _fsync_dir(directory)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class Writer:
    """Writes the Gatus file only when the meaningful content changes.

    Change detection ignores the timestamp header (via ``render_stable``) so an
    unchanged cluster never rewrites the file and never triggers a Gatus reload.
    """

    def __init__(self, output_path: str):
        self.output_path = output_path
        self._last_stable: str | None = None

    def reconcile(self, endpoints: list, external_endpoints: list | None = None) -> bool:
        external_endpoints = external_endpoints or []
        stable = render_stable(endpoints, external_endpoints)
        if stable == self._last_stable:
            return False
        _atomic_write(self.output_path, render(endpoints, external_endpoints))
        self._last_stable = stable
        logger.info(
            "wrote %d endpoint(s) + %d external endpoint(s) to %s",
            len(endpoints),
            len(external_endpoints),
            self.output_path,
        )
        return True
