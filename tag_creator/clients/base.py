from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from ..csv_store import CsvStore
from ..models import MediaFile, ProviderResult
from ..rate_limit import RateLimiter

LOGGER = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}


class CircuitBreaker:
    """Thread-safe per-provider breaker.

    After ``threshold`` consecutive failures a provider is tripped OPEN and every
    subsequent call for the rest of the run short-circuits (returns immediately)
    instead of wasting time on a host that is down. A success resets the counter.
    """

    def __init__(self, threshold: int = 5) -> None:
        self.threshold = max(1, threshold)
        self._failures: dict[str, int] = {}
        self._open: set[str] = set()
        self._lock = threading.Lock()

    def is_open(self, provider: str) -> bool:
        with self._lock:
            return provider in self._open

    def record_success(self, provider: str) -> None:
        with self._lock:
            self._failures[provider] = 0

    def record_failure(self, provider: str) -> bool:
        with self._lock:
            count = self._failures.get(provider, 0) + 1
            self._failures[provider] = count
            if count >= self.threshold and provider not in self._open:
                self._open.add(provider)
                LOGGER.warning(
                    "circuit breaker OPEN for %s after %s consecutive failures; skipping it for this run",
                    provider,
                    count,
                )
                return True
            return False


# One breaker shared by all clients for the process/run.
BREAKER = CircuitBreaker(threshold=max(1, int(os.environ.get("CIRCUIT_BREAKER_THRESHOLD", "5") or "5")))


class ProviderClient:
    provider_name = "base"
    base_url = ""
    cache_ttl_seconds = 60 * 60 * 24 * 14
    connect_timeout = 10.0
    read_timeout = 30.0
    post_read_timeout = 120.0

    def __init__(self, store: CsvStore, rate_limiter: RateLimiter) -> None:
        self.db = store
        self.rate_limiter = rate_limiter
        self.session = requests.Session()
        # Connection pool sized for the parallel pipeline; we do our own retry/
        # backoff so the adapter itself does not retry.
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def is_configured(self) -> bool:
        return True

    def enrich(self, media: MediaFile) -> ProviderResult | None:
        raise NotImplementedError

    @classmethod
    def _retry_delay(cls, response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isdigit():
            base = min(float(retry_after), 60.0)
        else:
            base = min(2.0**attempt, 30.0)
        # Full jitter so many workers don't retry in lockstep.
        return base + random.uniform(0.0, min(base, 2.0))

    @staticmethod
    def _backoff_sleep(attempt: int, ceiling: float = 15.0) -> None:
        base = min(2.0**attempt, ceiling)
        time.sleep(base + random.uniform(0.0, min(base, 2.0)))

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cache_key_extra: str = "",
    ) -> dict[str, Any] | None:
        params = params or {}
        headers = headers or {}
        cache_payload = {"url": url, "params": params, "extra": cache_key_extra}
        cache_key = hashlib.sha256(json.dumps(cache_payload, sort_keys=True).encode("utf-8")).hexdigest()
        cached = self.db.get_cache(self.provider_name, cache_key, self.cache_ttl_seconds)
        if cached is not None:
            return cached

        if BREAKER.is_open(self.provider_name):
            return None

        timeout = (self.connect_timeout, self.read_timeout)
        response: requests.Response | None = None
        for attempt in range(3):
            self.rate_limiter.wait(self.provider_name)
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                LOGGER.warning("%s request failed: %s", self.provider_name, exc)
                if attempt < 2:
                    self._backoff_sleep(attempt)
                    continue
                BREAKER.record_failure(self.provider_name)
                return None
            if response.status_code in _RETRY_STATUS and attempt < 2:
                LOGGER.warning("%s returned %s; retrying", self.provider_name, response.status_code)
                time.sleep(self._retry_delay(response, attempt))
                continue
            break
        if response is None:
            BREAKER.record_failure(self.provider_name)
            return None

        if response.status_code == 404:
            BREAKER.record_success(self.provider_name)
            self.db.set_cache(self.provider_name, cache_key, response.status_code, {})
            return {}
        if not response.ok:
            LOGGER.warning("%s returned %s: %s", self.provider_name, response.status_code, response.text[:200])
            BREAKER.record_failure(self.provider_name)
            return None

        try:
            data = response.json()
        except ValueError:
            LOGGER.warning("%s returned non-JSON response", self.provider_name)
            BREAKER.record_failure(self.provider_name)
            return None
        BREAKER.record_success(self.provider_name)
        self.db.set_cache(self.provider_name, cache_key, response.status_code, data)
        return data

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        cache_key_extra: str = "",
    ) -> dict[str, Any] | None:
        headers = headers or {}
        cache_payload = {"method": "POST", "url": url, "payload": payload, "extra": cache_key_extra}
        cache_key = hashlib.sha256(json.dumps(cache_payload, sort_keys=True).encode("utf-8")).hexdigest()
        cached = self.db.get_cache(self.provider_name, cache_key, self.cache_ttl_seconds)
        if cached is not None:
            return cached

        if BREAKER.is_open(self.provider_name):
            return None

        timeout = (self.connect_timeout, self.post_read_timeout)
        response: requests.Response | None = None
        for attempt in range(3):
            self.rate_limiter.wait(self.provider_name)
            try:
                response = self.session.post(url, json=payload, headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                LOGGER.warning("%s POST failed: %s", self.provider_name, exc)
                if attempt < 2:
                    self._backoff_sleep(attempt)
                    continue
                BREAKER.record_failure(self.provider_name)
                return None
            if response.status_code in _RETRY_STATUS and attempt < 2:
                LOGGER.warning("%s POST returned %s; retrying", self.provider_name, response.status_code)
                time.sleep(self._retry_delay(response, attempt))
                continue
            break
        if response is None:
            BREAKER.record_failure(self.provider_name)
            return None

        if not response.ok:
            LOGGER.warning("%s POST returned %s: %s", self.provider_name, response.status_code, response.text[:300])
            BREAKER.record_failure(self.provider_name)
            return None

        try:
            data = response.json()
        except ValueError:
            LOGGER.warning("%s POST returned non-JSON response", self.provider_name)
            BREAKER.record_failure(self.provider_name)
            return None
        BREAKER.record_success(self.provider_name)
        self.db.set_cache(self.provider_name, cache_key, response.status_code, data)
        return data


def first_existing(*values: str | None) -> str:
    for value in values:
        if value:
            return str(value).strip()
    return ""
