"""Global admission, circuit breaking, and graceful drain."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from fastapi import HTTPException
from prometheus_client import Gauge


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


CIRCUIT_STATE = Gauge(
    "gateway_circuit_state",
    "Circuit breaker state for an upstream backend (0=closed, 1=open, 2=half_open).",
    ["backend"],
)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


_STATE_GAUGE = {
    CircuitState.CLOSED: 0,
    CircuitState.OPEN: 1,
    CircuitState.HALF_OPEN: 2,
}


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
    """Consecutive-failure breaker with bounded half-open probes."""

    failure_threshold: int = field(
        default_factory=lambda: _env_int("GATEWAY_CIRCUIT_FAILURE_THRESHOLD", 5)
    )
    recovery_seconds: float = field(
        default_factory=lambda: _env_float("GATEWAY_CIRCUIT_RECOVERY_SECONDS", 30.0)
    )
    half_open_max_probes: int = field(
        default_factory=lambda: max(1, _env_int("GATEWAY_CIRCUIT_HALF_OPEN_PROBES", 1))
    )
    _failures: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)
    _state: dict[str, CircuitState] = field(default_factory=dict)
    _half_open_probes: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _clock: Callable[[], float] = field(default=time.monotonic)

    def _set_state(self, backend: str, state: CircuitState) -> None:
        self._state[backend] = state
        CIRCUIT_STATE.labels(backend=backend).set(_STATE_GAUGE[state])

    def state(self, backend: str) -> CircuitState:
        with self._lock:
            return self._state.get(backend, CircuitState.CLOSED)

    def allow(self, backend: str) -> bool:
        with self._lock:
            state = self._state.get(backend, CircuitState.CLOSED)
            if state == CircuitState.CLOSED:
                return True
            if state == CircuitState.OPEN:
                opened = self._opened_at.get(backend)
                if opened is None:
                    return True
                if self._clock() - opened < self.recovery_seconds:
                    return False
                # Enter half-open and admit the first probe slot.
                self._set_state(backend, CircuitState.HALF_OPEN)
                self._half_open_probes[backend] = 1
                return True
            # HALF_OPEN: admit up to N concurrent probes.
            used = self._half_open_probes.get(backend, 0)
            if used >= self.half_open_max_probes:
                return False
            self._half_open_probes[backend] = used + 1
            return True

    def record_success(self, backend: str) -> None:
        with self._lock:
            self._failures[backend] = 0
            self._opened_at.pop(backend, None)
            self._half_open_probes.pop(backend, None)
            self._set_state(backend, CircuitState.CLOSED)

    def record_failure(self, backend: str) -> None:
        with self._lock:
            state = self._state.get(backend, CircuitState.CLOSED)
            if state == CircuitState.HALF_OPEN:
                # Probe failed — reopen immediately.
                self._failures[backend] = self.failure_threshold
                self._opened_at[backend] = self._clock()
                self._half_open_probes[backend] = 0
                self._set_state(backend, CircuitState.OPEN)
                return
            failures = self._failures.get(backend, 0) + 1
            self._failures[backend] = failures
            if failures >= self.failure_threshold:
                self._opened_at[backend] = self._clock()
                self._half_open_probes[backend] = 0
                self._set_state(backend, CircuitState.OPEN)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "open": sorted(
                    backend for backend, state in self._state.items() if state == CircuitState.OPEN
                ),
                "half_open": sorted(
                    backend
                    for backend, state in self._state.items()
                    if state == CircuitState.HALF_OPEN
                ),
                "failures": dict(self._failures),
                "states": {backend: state.value for backend, state in self._state.items()},
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
