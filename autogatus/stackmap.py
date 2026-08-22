"""Optional service -> stack-name mapping, so endpoints group by stack.

Docker can't tell you the "stack" when everything runs under one compose
project, so an external map is the accurate source. The map is a flat
``{compose_service_name: stack}`` YAML/JSON file; anything not listed falls back
to a default group (or the container name).
"""

from __future__ import annotations

import logging

import yaml

logger = logging.getLogger("autogatus")


def load_stack_map(path: str) -> dict:
    if not path:
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("stack map %s not found; grouping by container name", path)
        return {}
    except Exception as e:
        logger.warning("failed to read stack map %s: %s", path, e)
        return {}
    mapping = data.get("stacks", data) if isinstance(data, dict) else {}
    logger.info("loaded stack map: %d services", len(mapping))
    return {str(k): str(v) for k, v in mapping.items()}
