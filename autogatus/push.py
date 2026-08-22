"""Push external-endpoint statuses to a Gatus instance."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger("autogatus")


class GatusPusher:
    def __init__(self, base_url: str, token: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session = requests.Session()

    def push(self, key: str, success: bool, error: str = "", duration=None) -> bool:
        """POST one status. ``key`` is the external endpoint key (group_name).

        Gatus takes ``duration`` as a Go duration; we send it in milliseconds
        (e.g. ``42ms``) so a percentage headline reads as "42" on the graph.
        A 404 means Gatus has not loaded the declaration yet (freshly added
        container); the caller simply retries next cycle.
        """
        params = {"success": "true" if success else "false"}
        if error:
            params["error"] = error[:500]
        if duration is not None:
            params["duration"] = f"{int(round(float(duration)))}ms"
        url = f"{self.base_url}/api/v1/endpoints/{key}/external"
        try:
            resp = self._session.post(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
        except Exception as e:
            logger.warning("push failed for %s: %s", key, e)
            return False
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            logger.debug("endpoint %s not registered yet (will retry)", key)
            return False
        logger.warning("push %s -> HTTP %s: %s", key, resp.status_code, resp.text[:120])
        return False
