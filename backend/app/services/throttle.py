"""Login throttling.

This is the front door to a service that can decrypt every router credential in
the fleet, so unlimited password guessing is not acceptable.

Counters live in memory. That is a deliberate limit, not an oversight: Redis
would survive a restart and be shared across replicas, but it also puts the auth
path on a dependency that, when it is down, either fails every login or silently
stops throttling. In-memory degrades honestly -- a restart forgives attempts,
and each replica enforces its own budget -- and for a controller with a handful
of operators that is the better trade. Move it to Redis when there are enough
replicas that per-replica budgets stop meaning anything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class _Bucket:
    failures: int = 0
    locked_until: float = 0.0
    first_failure: float = 0.0


@dataclass
class LoginThrottle:
    max_attempts: int = 5
    lockout_seconds: int = 300
    # Failures older than this stop counting, so someone who mistypes twice a
    # week is never locked out.
    window_seconds: int = 900

    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def check(self, key: str) -> float:
        """Seconds remaining on a lockout, or 0.0 if the caller may proceed."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return 0.0
            if bucket.locked_until > now:
                return bucket.locked_until - now
            if bucket.locked_until:
                # Lockout expired: start clean rather than locking again on the
                # next single mistake.
                del self._buckets[key]
            return 0.0

    def record_failure(self, key: str) -> float:
        """Count a failed attempt. Returns the lockout imposed, if any."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.setdefault(key, _Bucket(first_failure=now))
            if now - bucket.first_failure > self.window_seconds:
                bucket.failures = 0
                bucket.first_failure = now
            bucket.failures += 1
            if bucket.failures >= self.max_attempts:
                bucket.locked_until = now + self.lockout_seconds
                return float(self.lockout_seconds)
            return 0.0

    def record_success(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_throttle: LoginThrottle | None = None


def get_throttle() -> LoginThrottle:
    global _throttle
    if _throttle is None:
        from app.config import get_settings

        settings = get_settings()
        _throttle = LoginThrottle(
            max_attempts=settings.login_max_attempts,
            lockout_seconds=settings.login_lockout_seconds,
        )
    return _throttle


def throttle_key(email: str, source_ip: str | None) -> str:
    """Bucket on both the account and the source.

    Keying on the account alone lets anyone lock a colleague out by guessing at
    their address. Keying on the source alone lets a botnet spread its guesses.
    Combining them means an attacker must burn a fresh source per account.
    """
    return f"{email.strip().lower()}|{source_ip or 'unknown'}"
