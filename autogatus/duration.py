"""Parse time values into seconds.

Everything time-valued in autogatus takes a Go-style duration string (``30s``,
``5m``, ``1h30m``), the same format Gatus uses in its own config, so a homelab
owner never switches formats between the two. A bare number is still read as
seconds so old configs keep working.
"""

from __future__ import annotations

import re

# One or more <number><unit> parts, e.g. "90s", "1h30m", "500ms". A bare number
# (no unit) is treated as seconds.
_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)?")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "": 1.0}


def parse_duration_seconds(value, default: float) -> float:
    """Return ``value`` in seconds, or ``default`` if it cannot be parsed.

    Accepts a bare number (seconds), a single unit (``30s``, ``5m``, ``2h``,
    ``500ms``), or a compound Go duration (``1h30m``).
    """
    s = str(value if value is not None else "").strip().lower()
    if not s:
        return default
    # A bare number stays seconds.
    try:
        return float(s)
    except ValueError:
        pass
    total = 0.0
    matched = False
    pos = 0
    for m in _PART.finditer(s):
        if m.start() != pos:  # a gap means junk between parts, reject the whole value
            return default
        pos = m.end()
        total += float(m.group(1)) * _UNIT_SECONDS[m.group(2) or ""]
        matched = True
    if not matched or pos != len(s):
        return default
    return total
