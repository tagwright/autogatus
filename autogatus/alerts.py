"""Alert-channel routing: turn requested provider types into a Gatus ``alerts``
block, validated against the providers Gatus actually has configured.

autogatus never sends notifications itself. It only names Gatus alert providers
in the ``alerts:`` block of each endpoint/external-endpoint it writes. Gatus owns
the sending. The provider MUST be configured in Gatus's ``alerting:`` section for
anything to fire; these helpers derive that configured set so a label naming an
unconfigured provider is dropped with a warning instead of silently dying.
"""

from __future__ import annotations

import logging
import os

import yaml

logger = logging.getLogger("autogatus")

# Values that mean "no alerts" when given to a *.alerts label/env.
_NONE = {"", "none", "false", "off", "no"}


def sanitize_description(text: str) -> str:
    """Gatus rejects a `"` or `\\` in an alert description and fails the whole
    config load, so scrub them from anything that flows into a description."""
    return (text or "").replace('"', "").replace("\\", "")


def parse_type_list(value):
    """Parse a comma list of provider types.

    Returns ``None`` when the value is absent (caller falls back to a default),
    or a list of type names. ``none``/empty/``false`` yield ``[]`` (explicitly no
    alerts). Order is preserved, duplicates removed.
    """
    if value is None:
        return None
    if str(value).strip().lower() in _NONE:
        return []
    seen = []
    for raw in str(value).split(","):
        t = raw.strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def configured_providers(path: str, skip_basename: str = "") -> set:
    """Union of the keys under the top-level ``alerting:`` section across Gatus's
    config (a single file or a directory of merged files).

    Only KEYS (provider names) are read, never their values, so no secrets are
    touched or logged. Returns an empty set if nothing is readable.
    """
    if not path:
        return set()
    files = []
    if os.path.isdir(path):
        for root, _dirs, names in os.walk(path):
            for n in names:
                if n.endswith((".yaml", ".yml")) and n != skip_basename:
                    files.append(os.path.join(root, n))
    elif os.path.isfile(path):
        files = [path]
    else:
        return set()

    providers = set()
    for fp in files:
        try:
            with open(fp) as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.debug("could not read gatus config %s: %s", fp, e)
            continue
        if isinstance(data, dict):
            alerting = data.get("alerting")
            if isinstance(alerting, dict):
                providers.update(str(k) for k in alerting.keys())
    return providers


def resolve_allowlist(gatus_config_path: str, env_types: list, skip_basename: str = "") -> tuple:
    """Return ``(allowlist_set, source_label)``.

    Precedence: providers configured in Gatus (primary) -> AUTOGATUS_ALERT_TYPES
    (fallback) -> ``{custom}`` (last resort).
    """
    configured = configured_providers(gatus_config_path, skip_basename=skip_basename)
    if configured:
        return configured, f"gatus-config ({gatus_config_path})"
    if env_types:
        return set(env_types), "AUTOGATUS_ALERT_TYPES"
    return {"custom"}, "default (custom)"


def build_alerts(types, description: str) -> list:
    """Build a list of Gatus alert dicts from provider type names."""
    desc = sanitize_description(description)
    return [{"type": t, "description": desc} for t in types]


def filter_alerts(alerts, allowlist: set, context: str) -> list:
    """Drop alerts whose provider type is not in ``allowlist``, warning per drop.

    ``alerts`` is a list of ``{type, description}`` dicts. Returns the kept list
    (possibly empty).
    """
    if not alerts:
        return []
    kept, dropped = [], []
    for a in alerts:
        (kept if a.get("type") in allowlist else dropped).append(a)
    if dropped:
        names = ",".join(a.get("type", "?") for a in dropped)
        logger.warning(
            "alerts: dropping unconfigured provider(s) %s for %s (not in Gatus alerting config)",
            names,
            context,
        )
    return kept
