"""Parse ``gatus.*`` container labels into Gatus endpoint definitions.

Pure functions, no Docker dependency, so the whole compilation step is unit
testable from a plain dict of labels.

Label schema (Traefik/Autokuma shaped, opt-in):

    gatus.enable=true                       # required, per container
    gatus.<id>.url=http://svc:3000/health   # required, per endpoint
    gatus.<id>.name=web                      # default: <id>
    gatus.<id>.group=geoducking              # default: the container name
    gatus.<id>.interval=60s                  # default: 60s
    gatus.<id>.conditions.0=[STATUS] == 200  # indexed; sensible default by scheme
    gatus.<id>.conditions.1=[RESPONSE_TIME] < 500
    gatus.<id>.method=GET                     # optional (http)
    gatus.<id>.body=...                       # optional (http)
    gatus.<id>.headers.X-Api-Key=...          # optional (http), repeatable
    gatus.<id>.alert=true                     # default: true -> attaches a custom alert
    gatus.<id>.alerts=custom,ntfy             # list of Gatus providers (wins over .alert)
    gatus.<id>.alert-description=...           # default: "<name> is down"

One container may declare many endpoints by using different ``<id>`` segments.
"""

from __future__ import annotations

import re

from .alerts import build_alerts, parse_type_list

ENABLE_KEY = "gatus.enable"
PREFIX = "gatus."

_TRUE = {"true", "1", "yes", "on"}


def is_enabled(labels: dict) -> bool:
    """Whether a container opted in with ``gatus.enable=true``."""
    return str(labels.get(ENABLE_KEY, "")).strip().lower() in _TRUE


def _default_conditions(url: str) -> list:
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme in ("http", "https"):
        return ["[STATUS] == 200"]
    if scheme in ("tcp", "udp"):
        return ["[CONNECTED] == true"]
    if scheme in ("icmp",):
        return ["[CONNECTED] == true"]
    return []


def _group_endpoint_labels(labels: dict) -> dict:
    """Bucket ``gatus.<id>.<rest>`` labels by ``<id>``.

    Returns ``{id: {rest: value}}``. The reserved ``gatus.enable`` key and any
    bare ``gatus.<x>`` with no further segment are ignored.
    """
    buckets: dict = {}
    for key, value in labels.items():
        if not key.startswith(PREFIX) or key == ENABLE_KEY:
            continue
        remainder = key[len(PREFIX) :]
        if "." not in remainder:
            # e.g. "gatus.enable" already handled; a bare "gatus.foo" is not a
            # valid endpoint field, so skip it rather than guess.
            continue
        endpoint_id, field = remainder.split(".", 1)
        if not endpoint_id:
            continue
        buckets.setdefault(endpoint_id, {})[field] = value
    return buckets


def _collect_conditions(fields: dict) -> list:
    """Pull ``conditions.<n>`` fields, ordered by their numeric index."""
    found = []
    for field, value in fields.items():
        m = re.fullmatch(r"conditions\.(\d+)", field)
        if m:
            found.append((int(m.group(1)), value))
    return [v for _, v in sorted(found, key=lambda t: t[0])]


def _collect_headers(fields: dict) -> dict:
    headers = {}
    for field, value in fields.items():
        if field.startswith("headers."):
            headers[field[len("headers.") :]] = value
    return headers


def parse_container(labels: dict, container_name: str, default_group: str = "") -> list:
    """Compile one container's labels into a list of endpoint dicts.

    Returns ``[]`` if the container is not enabled or declares no valid endpoint.
    Raises ``ValueError`` if an endpoint is missing its required ``url``.
    """
    if not is_enabled(labels):
        return []

    group_fallback = default_group or container_name
    endpoints = []

    for endpoint_id, fields in sorted(_group_endpoint_labels(labels).items()):
        url = fields.get("url", "").strip()
        if not url:
            raise ValueError(
                f"container '{container_name}': endpoint '{endpoint_id}' "
                f"has no gatus.{endpoint_id}.url"
            )

        name = fields.get("name", endpoint_id).strip() or endpoint_id
        group = fields.get("group", group_fallback).strip() or group_fallback
        interval = fields.get("interval", "60s").strip() or "60s"

        conditions = _collect_conditions(fields) or _default_conditions(url)

        endpoint = {
            "name": name,
            "group": group,
            "url": url,
            "interval": interval,
            "conditions": conditions,
        }

        method = fields.get("method", "").strip()
        if method:
            endpoint["method"] = method
        body = fields.get("body", "")
        if body:
            endpoint["body"] = body
        headers = _collect_headers(fields)
        if headers:
            endpoint["headers"] = headers

        # Alert channels: `gatus.<id>.alerts=custom,ntfy` (list of provider
        # types) wins if present; otherwise the legacy `gatus.<id>.alert` bool
        # (true/absent -> custom, false -> none). These are the REQUESTED types;
        # they are filtered against Gatus's configured providers downstream.
        requested = parse_type_list(fields.get("alerts"))
        if requested is None:
            alert = str(fields.get("alert", "true")).strip().lower() in _TRUE
            requested = ["custom"] if alert else []
        if requested:
            description = fields.get("alert-description", "").strip() or f"{name} is down"
            endpoint["alerts"] = build_alerts(requested, description)

        endpoints.append(endpoint)

    return endpoints
