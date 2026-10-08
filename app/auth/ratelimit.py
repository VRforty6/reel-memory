"""Login brute-force protection: in-memory per-IP + per-account throttles.

Pure-ish and testable: the clock is injectable, state lives in a small
class (no globals) so tests get a fresh limiter per case.

NOTE: state is per-process. Behind multiple API workers an attacker gets
`workers x limit` attempts — acceptable for this threat model, but a Redis
(or DB) backed limiter is the upgrade path; the class boundary makes that
a drop-in swap. The README documents this.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from app.config import settings


class RateLimited(Exception):
    """A login attempt was throttled. Carries `retry_after_s` for the API."""

    def __init__(self, retry_after_s: int):
        super().__init__("too many login attempts — try again shortly")
        self.retry_after_s = retry_after_s


@dataclass
class _Bucket:
    attempts: deque  # timestamps (monotonic) of recent failures


class LoginRateLimiter:
    def __init__(
        self,
        max_attempts_ip: int | None = None,
        max_attempts_account: int | None = None,
        window_s: int | None = None,
        clock=time.monotonic,
    ):
        self.max_attempts_ip = max_attempts_ip or settings.login_max_attempts_ip
        self.max_attempts_account = (
            max_attempts_account if max_attempts_account is not None
            else settings.login_max_attempts_account
        )
        self.window_s = window_s or settings.login_window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def _prune(self, bucket: _Bucket, now: float) -> None:
        cutoff = now - self.window_s
        attempts = bucket.attempts
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()

    def check(self, ip: str, email: str) -> None:
        """Raise RateLimited if this login may not proceed. Call BEFORE any
        credential verification so enumeration cost stays constant."""
        now = self._clock()
        keys = [(f"ip:{ip}", self.max_attempts_ip), (f"acct:{email}", self.max_attempts_account)]
        with self._lock:
            for key, limit in keys:
                bucket = self._buckets.get(key)
                if bucket is None:
                    continue
                self._prune(bucket, now)
                if len(bucket.attempts) >= limit:
                    oldest = bucket.attempts[0]
                    retry_after = max(1, int(oldest + self.window_s - now) + 1)
                    raise RateLimited(retry_after)

    def record_failure(self, ip: str, email: str) -> None:
        now = self._clock()
        with self._lock:
            for key in (f"ip:{ip}", f"acct:{email}"):
                bucket = self._buckets.setdefault(key, _Bucket(deque()))
                self._prune(bucket, now)
                bucket.attempts.append(now)

    def record_success(self, ip: str, email: str) -> None:
        """A successful login clears the account's failure history (but not
        the IP's — a shared IP hammering many accounts stays throttled)."""
        with self._lock:
            self._buckets.pop(f"acct:{email}", None)


# Process-wide limiter used by the API layer.
login_limiter = LoginRateLimiter()
