from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, delays: dict[str, float]) -> None:
        self.delays = delays
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, provider: str) -> None:
        delay = self.delays.get(provider, 0)
        if delay <= 0:
            return
        with self._lock:
            now = time.time()
            last_call = self._last_call.get(provider, 0)
            sleep_for = delay - (now - last_call)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last_call[provider] = time.time()

