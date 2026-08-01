"""Global admission, circuit breaking, and graceful drain."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

from fastapi import HTTPException


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return max(0, int(raw))


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return max(0.0, float(raw))


@dataclass
class DrainState:
    draining: bool = False
    started_at: float | None = None

    def begin(self) -> None:
        if self.draining:
            return
        self.draining = True
        self.started_at = time.time()

    def reject_if_draining(self) -> None:
        if self.draining:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "api_error",
                        "code": "gateway_draining",
                        "message": "gateway is draining and not accepting new requests",
                    }
                },
                headers={"Retry-After": "1"},
            )


@dataclass
class GlobalAdmissionController:
    """Process-wide inflight/queue limits for load shedding."""

    max_inflight: int = field(default_factory=lambda: _env_int("GATEWAY_MAX_INFLIGHT", 256))
    max_queued: int = field(default_factory=lambda: _env_int("GATEWAY_MAX_QUEUED", 256))
    _sem: asyncio.Semaphore | None = None
    _queued: int = 0
    _inflight: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(max(1, self.max_inflight))

    async def acquire(self) -> None:
        assert self._sem is not None
        async with self._lock:
            if self._inflight >= self.max_inflight and self._queued >= self.max_queued:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": {
                            "type": "rate_limit_error",
                            "code": "gateway_load_shed",
                            "message": "gateway capacity exceeded; request shed",
                        }
                    },
                    headers={
                        "Retry-After": "1",
                        "X-RateLimit-Limit": str(self.max_inflight),
                        "X-RateLimit-Remaining": "0",
                    },
                )
            self._queued += 1
        acquired = False
        try:
            await self._sem.acquire()
            acquired = True
        finally:
            async with self._lock:
                self._queued = max(0, self._queued - 1)
                if acquired:
                    self._inflight += 1

    async def release(self) -> None:
        assert self._sem is not None
        self._sem.release()
        async with self._lock:
            self._inflight = max(0, self._inflight - 1)


@dataclass
class CircuitBreaker:
    """Simple consecutive-failure breaker for one upstream backend."""

    failure_threshold: int = field(
        default_factory=lambda: _env_int("GATEWAY_CIRCUIT_FAILURE_THRESHOLD", 5)
    )
    recovery_seconds: float = field(
        default_factory=lambda: _env_float("GATEWAY_CIRCUIT_RECOVERY_SECONDS", 30.0)
    )
    _failures: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)

    def allow(self, backend: str) -> bool:
        opened = self._opened_at.get(backend)
        if opened is None:
            return True
        # Half-open probe after recovery window.
        return time.time() - opened >= self.recovery_seconds

    def record_success(self, backend: str) -> None:
        self._failures[backend] = 0
        self._opened_at.pop(backend, None)

    def record_failure(self, backend: str) -> None:
        failures = self._failures.get(backend, 0) + 1
        self._failures[backend] = failures
        if failures >= self.failure_threshold:
            self._opened_at[backend] = time.time()

    def snapshot(self) -> dict[str, object]:
        return {
            "open": sorted(self._opened_at),
            "failures": dict(self._failures),
        }


@dataclass
class RetryBudget:
    """Bound fallback retries across the process."""

    max_retries_per_minute: int = field(
        default_factory=lambda: _env_int("GATEWAY_RETRY_BUDGET_PER_MINUTE", 60)
    )
    _window_start: float = field(default_factory=time.monotonic)
    _used: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def consume(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            if now - self._window_start >= 60:
                self._window_start = now
                self._used = 0
            if self._used >= self.max_retries_per_minute:
                return False
            self._used += 1
            return True
