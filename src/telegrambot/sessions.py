"""Session inactivity timeout.

An open session holds nothing but Telegram file_ids in memory -- no upload
happens until /finish_job -- so expiring one needs no rollback in R2. Dropping
the state is the whole cleanup.

The timer is re-armed on every screenshot, so it measures inactivity rather
than total session length: a slow batch is not killed halfway through.
"""

import threading
from typing import Callable


class SessionTimers:
    def __init__(self, ttl_seconds: int, on_expire: Callable[[int, int], None]):
        self._ttl = ttl_seconds
        self._on_expire = on_expire
        self._timers: dict[int, threading.Timer] = {}
        self._lock = threading.Lock()

    @property
    def ttl_minutes(self) -> int:
        return self._ttl // 60

    def arm(self, chat_id: int, user_id: int) -> None:
        """Start (or restart) the countdown for a user."""
        with self._lock:
            self._cancel_locked(user_id)
            t = threading.Timer(self._ttl, self._fire, args=(chat_id, user_id))
            t.daemon = True  # dies with the process; no hanging shutdown
            self._timers[user_id] = t
            t.start()

    def disarm(self, user_id: int) -> None:
        """Stop the countdown -- call this when a session closes properly."""
        with self._lock:
            self._cancel_locked(user_id)

    def _cancel_locked(self, user_id: int) -> None:
        t = self._timers.pop(user_id, None)
        if t is not None:
            t.cancel()

    def _fire(self, chat_id: int, user_id: int) -> None:
        with self._lock:
            self._timers.pop(user_id, None)
        self._on_expire(chat_id, user_id)