"""Gather endpoints from running Docker containers and write the Gatus file."""

from __future__ import annotations

import logging
import os
import tempfile

from .alerts import filter_alerts
from .labels import parse_container
from .render import render, render_stable

logger = logging.getLogger("autogatus")


def gather_endpoints(client, default_group: str = "", allowlist=None) -> list:
    """Compile endpoints from every running, gatus-enabled container.

    A single container with a malformed endpoint (e.g. missing url) is logged
    and skipped rather than taking the whole reconcile down. Requested alert
    channels are filtered against ``allowlist`` (the providers Gatus has
    configured); unconfigured ones are dropped with a warning.
    """
    endpoints = []
    for container in client.containers.list():
        labels = container.labels or {}
        name = container.name
        try:
            found = parse_container(labels, container_name=name, default_group=default_group)
        except ValueError as e:
            logger.warning("skipping container %s: %s", name, e)
            continue
        for ep in found:
            if allowlist is not None and "alerts" in ep:
                kept = filter_alerts(ep["alerts"], allowlist, f"{ep['group']}/{ep['name']}")
                if kept:
                    ep["alerts"] = kept
                else:
                    ep.pop("alerts")
        if found:
            logger.debug("container %s -> %d endpoint(s)", name, len(found))
            endpoints.extend(found)
    return endpoints


def _atomic_write(path: str, content: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".autogatus-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
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
        self._last_stable = None

    def reconcile(self, endpoints: list, external_endpoints: list = None) -> bool:
        external_endpoints = external_endpoints or []
        stable = render_stable(endpoints, external_endpoints)
        if stable == self._last_stable:
            return False
        _atomic_write(self.output_path, render(endpoints, external_endpoints))
        self._last_stable = stable
        logger.info(
            "wrote %d endpoint(s) + %d external endpoint(s) to %s",
            len(endpoints), len(external_endpoints), self.output_path,
        )
        return True
