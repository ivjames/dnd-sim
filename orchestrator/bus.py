"""Fan-out event bus for spectators (SSE, CLI, tests). CONTRACTS.md §4."""

from __future__ import annotations

import queue
import threading
from typing import Any

__all__ = ["EventBus"]


class EventBus:
    """Thread-safe publish/subscribe with replayable history.

    `publish` never blocks the game thread: a subscriber queue that is full is
    dropped rather than allowed to stall the loop.
    """

    def __init__(self, max_history: int = 5000, queue_size: int = 2000) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._history: list[Any] = []
        self._max_history = max_history
        self._queue_size = queue_size
        self.closed = False

    def subscribe(self) -> "queue.Queue":
        q: queue.Queue = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subs.append(q)
            if self.closed:
                q.put_nowait(None)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, ev: Any) -> None:
        with self._lock:
            self._history.append(ev)
            if len(self._history) > self._max_history:
                del self._history[: len(self._history) - self._max_history]
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                self.unsubscribe(q)

    def close(self) -> None:
        """Signal end-of-stream (None) to every subscriber."""
        with self._lock:
            self.closed = True
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def history(self) -> list:
        with self._lock:
            return list(self._history)
