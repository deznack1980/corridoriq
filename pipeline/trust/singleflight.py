"""Small in-process LRU cache where concurrent misses for one key compute once."""

from __future__ import annotations

import threading
from collections import OrderedDict


class SingleFlightCache:
    def __init__(self, max_entries: int = 16):
        self.max_entries = max_entries
        self._entries: "OrderedDict[tuple, object]" = OrderedDict()
        self._inflight: dict[tuple, threading.Event] = {}
        self._lock = threading.Lock()
        self.stats = {"hits": 0, "misses": 0, "waits": 0}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            for key in self.stats:
                self.stats[key] = 0

    def peek(self, key: tuple) -> bool:
        with self._lock:
            return key in self._entries

    def get_or_compute(self, key: tuple | None, compute):
        """Cached value for key, computed once. Concurrent callers with the same
        key wait for the first computation. Callers must not mutate the value."""
        if key is None:
            return compute()
        while True:
            with self._lock:
                if key in self._entries:
                    self._entries.move_to_end(key)
                    self.stats["hits"] += 1
                    return self._entries[key]
                event = self._inflight.get(key)
                owner = event is None
                if owner:
                    event = threading.Event()
                    self._inflight[key] = event
                    self.stats["misses"] += 1
                else:
                    self.stats["waits"] += 1
            if not owner:
                event.wait()
                continue  # re-check: the owner may have failed and stored nothing
            try:
                value = compute()
                with self._lock:
                    self._entries[key] = value
                    self._entries.move_to_end(key)
                    while len(self._entries) > self.max_entries:
                        self._entries.popitem(last=False)
                return value
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                event.set()
