"""Thin wrapper around the TypeSafe SDK with an on-disk cache.

The cache avoids re-spending tokens/money when re-running segmentation on the
same CSV (e.g. while tuning thresholds). Keyed by a hash of the exact request
payload (state + questions), so any change in wording invalidates it safely.
"""

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping

from typesafe_sdk import Choice, Noul, RetryPolicy, Score, TypeSafeClient

logger = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path(".typesafe_cache.json")

# How many live (non-cached) API calls to make before flushing the cache to
# disk. On a long run over tens of thousands of messages this means a crash,
# Ctrl-C, or network outage only loses a handful of already-paid-for calls
# instead of the whole run's progress.
FLUSH_EVERY_N_CALLS = 20

# Per-request timeout and retry behavior, so a slow/unavailable TypeSafe
# server doesn't hang the whole run and we don't hammer it with unbounded
# retries. The SDK already honors `Retry-After` headers on 429s.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRY_POLICY = RetryPolicy(max_retries=4, backoff_initial=1.0, backoff_max=20.0, timeout=60.0)

# TypeSafe's published limit is 250,000 tokens/sec and 1,200 requests/minute.
# We target a conservative fraction of the request-rate limit (leaving
# headroom for retries and any other concurrent usage of the same API key),
# rather than the flat fixed per-call sleep this used to be. Concurrent
# workers (see --concurrency) share this single global rate limiter, so
# raising concurrency increases throughput up to this RPM ceiling instead of
# just running more sleeps in parallel.
DEFAULT_MAX_REQUESTS_PER_MINUTE = 500


class _RateLimiter:
    """Thread-safe pacing so combined callers don't exceed N requests/minute.

    Spaces out call *starts* by 60/N seconds using a lock-protected
    timestamp, which keeps concurrent worker threads collectively under the
    configured rate regardless of how many of them there are.
    """

    def __init__(self, max_requests_per_minute: float):
        self._min_interval = 60.0 / max_requests_per_minute if max_requests_per_minute > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_allowed_at)
            self._next_allowed_at = start_at + self._min_interval
        delay = start_at - now
        if delay > 0:
            time.sleep(delay)


def _question_to_dict(q) -> Dict[str, Any]:
    return q.model_dump(exclude_none=True)


def _hash_request(state: Any, questions: Mapping[str, Any]) -> str:
    payload = {
        "state": state,
        "questions": {k: _question_to_dict(v) for k, v in questions.items()},
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class CachedTypeSafeClient:
    """Wraps TypeSafeClient.system_one() with a persistent JSON cache."""

    def __init__(
        self,
        cache_path: Path | str = DEFAULT_CACHE_PATH,
        disable_cache: bool = False,
        max_requests_per_minute: float = DEFAULT_MAX_REQUESTS_PER_MINUTE,
    ):
        api_key = os.getenv("TYPESAFE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "TYPESAFE_API_KEY is not set. Add it to your .env file "
                "(get one at https://console.typesafe.ai/settings/keys)."
            )
        self._client = TypeSafeClient(
            api_key=api_key,
            timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            retry=DEFAULT_RETRY_POLICY,
        )
        self._rate_limiter = _RateLimiter(max_requests_per_minute)
        self.cache_path = Path(cache_path)
        self.disable_cache = disable_cache
        self._cache: Dict[str, Any] = {}
        self._dirty = False
        self._calls_since_flush = 0
        # Guards _cache/_dirty/_calls_since_flush against concurrent worker
        # threads calling system_one() at the same time (see --concurrency).
        self._state_lock = threading.Lock()
        if not disable_cache and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Could not read TypeSafe cache at %s, starting fresh.", self.cache_path)
                self._cache = {}

    def system_one(self, state: Any, questions: Mapping[str, Any]) -> Dict[str, Any]:
        key = _hash_request(state, questions)
        if not self.disable_cache:
            with self._state_lock:
                cached = self._cache.get(key)
            if cached is not None:
                return cached

        self._rate_limiter.wait()
        response = self._client.system_one(state=state, questions=questions)
        result = {
            "model": response.model,
            "answers": {
                name: answer.model_dump(exclude_none=True)
                for name, answer in response.answers.items()
            },
        }
        should_flush = False
        with self._state_lock:
            self._cache[key] = result
            self._dirty = True
            self._calls_since_flush += 1
            if self._calls_since_flush >= FLUSH_EVERY_N_CALLS:
                should_flush = True
                self._calls_since_flush = 0
        if should_flush:
            self.flush()
        return result

    def flush(self):
        with self._state_lock:
            if self.disable_cache or not self._dirty:
                return
            snapshot = dict(self._cache)
            self._dirty = False
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.flush()
        self._client.close()


__all__ = ["CachedTypeSafeClient", "Choice", "Noul", "Score"]
