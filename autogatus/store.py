"""Thread-safe store of the latest container health, read by the web view.

The monitor loop writes a snapshot each cycle; the Flask view reads it. Also
keeps a short rolling history of success/fail per key so the detail page can
draw Gatus-style status squares.
"""

from __future__ import annotations

import threading
from collections import deque


class Store:
    def __init__(self, history: int = 50):
        self._lock = threading.Lock()
        self._items = {}
        self._history = history

    def update(self, key, stack, name, health, verdict, ts):
        with self._lock:
            item = self._items.get(key)
            if item is None:
                item = {"history": deque(maxlen=self._history)}
                self._items[key] = item
            item["stack"] = stack
            item["name"] = name
            item["health"] = health
            item["verdict"] = verdict
            item["updated"] = ts
            item["history"].append((bool(verdict.success), ts))

    def prune(self, present_keys):
        with self._lock:
            for k in [k for k in self._items if k not in present_keys]:
                del self._items[k]

    def get(self, key):
        with self._lock:
            item = self._items.get(key)
            return dict(item, history=list(item["history"])) if item else None

    def all(self):
        with self._lock:
            return {k: dict(v, history=list(v["history"])) for k, v in self._items.items()}
