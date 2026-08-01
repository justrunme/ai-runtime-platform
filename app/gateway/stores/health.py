"""Backend health probing and shared health state."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

import httpx

from app.gateway.config import GatewaySettings, ModelTarget
from app.gateway.services.urls import backend_health_url


@dataclass
class BackendHealth:
    available: bool | None = None
    latency_ms: float | None = None
    request_count: int = 0
    error_count: int = 0
    fallback_count: int = 0
    ewma_error_rate: float = 0.0
    consecutive_failures: int = 0
    updated_at: float = 0.0

    def score(self) -> int:
        if self.available is False:
            return 0
        # Prefer EWMA so short outages stop dominating forever.
        error_rate = self.ewma_error_rate if self.request_count else 0.0
        latency_penalty = min((self.latency_ms or 0) / 20, 30)
        error_penalty = error_rate * 50
        fallback_penalty = self.fallback_rate * 20
        score = round(max(0, 100 - latency_penalty - error_penalty - fallback_penalty))
        if self.consecutive_failures >= 3:
            return min(score, 20)
        return score

    @property
    def error_rate(self) -> float:
        return self.ewma_error_rate if self.request_count else 0.0

    @property
    def fallback_rate(self) -> float:
        return self.fallback_count / self.request_count if self.request_count else 0.0


def _apply_request_signal(health: BackendHealth, *, success: bool, fallback_used: bool) -> None:
    alpha = 0.3
    sample = 0.0 if success else 1.0
    health.request_count += 1
    health.error_count += int(not success)
    health.fallback_count += int(fallback_used)
    health.ewma_error_rate = alpha * sample + (1 - alpha) * health.ewma_error_rate
    health.consecutive_failures = 0 if success else health.consecutive_failures + 1
    health.updated_at = time.time()


def _format_health_row(model: str, target: ModelTarget, health: BackendHealth) -> dict[str, object]:
    status = (
        "healthy" if health.available else "unhealthy" if health.available is False else "unknown"
    )
    return {
        "name": target.backend_name or model,
        "model": model,
        "status": status,
        "score": health.score(),
        "latency_ms": health.latency_ms,
        "error_rate": round(health.error_rate, 4),
        "fallback_rate": round(health.fallback_rate, 4),
    }


class HealthStore:
    """Base store: probing is shared, persistence is implementation-specific."""

    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def probe_all(self) -> None:
        await asyncio.gather(
            *(self.probe(model, target) for model, target in self._settings.model_targets.items())
        )

    async def probe(self, model: str, target: ModelTarget) -> None:
        started_at = time.monotonic()
        try:
            response = await self._client.get(
                backend_health_url(target), timeout=min(5, self._settings.timeout_seconds)
            )
            available = response.is_success
        except httpx.RequestError:
            available = False
        latency_ms = round((time.monotonic() - started_at) * 1000, 2)
        await self._store_probe(model, available, latency_ms)

    async def snapshot(self) -> list[dict[str, object]]:
        return [
            _format_health_row(model, target, await self._load(model))
            for model, target in self._settings.model_targets.items()
        ]

    async def meets_score(self, model: str, threshold: int) -> bool:
        health = await self._load(model)
        return health.available is not False and health.score() >= threshold

    async def routing_signal(self, model: str) -> tuple[int, float | None, bool | None]:
        health = await self._load(model)
        return health.score(), health.latency_ms, health.available

    async def aclose(self) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def _store_probe(self, model: str, available: bool, latency_ms: float) -> None:
        raise NotImplementedError

    async def record_request(
        self, model: str, *, success: bool, fallback_used: bool = False
    ) -> None:
        raise NotImplementedError

    async def _load(self, model: str) -> BackendHealth:
        raise NotImplementedError


class BackendHealthStore(HealthStore):
    """In-memory health signals scoped to one gateway replica."""

    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient) -> None:
        super().__init__(settings, client)
        self._health = {model: BackendHealth() for model in settings.model_targets}
        self._lock = asyncio.Lock()

    async def _store_probe(self, model: str, available: bool, latency_ms: float) -> None:
        async with self._lock:
            self._health[model].available = available
            self._health[model].latency_ms = latency_ms

    async def record_request(
        self, model: str, *, success: bool, fallback_used: bool = False
    ) -> None:
        async with self._lock:
            _apply_request_signal(self._health[model], success=success, fallback_used=fallback_used)

    async def _load(self, model: str) -> BackendHealth:
        async with self._lock:
            current = self._health[model]
            return BackendHealth(
                available=current.available,
                latency_ms=current.latency_ms,
                request_count=current.request_count,
                error_count=current.error_count,
                fallback_count=current.fallback_count,
                ewma_error_rate=current.ewma_error_rate,
                consecutive_failures=current.consecutive_failures,
                updated_at=current.updated_at,
            )


class RedisHealthStore(HealthStore):
    """Fleet-wide health signals shared by all gateway replicas through Redis."""

    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient, redis: Redis) -> None:
        super().__init__(settings, client)
        self._redis = redis

    @staticmethod
    def _key(model: str) -> str:
        return f"arp:health:{model}"

    async def _store_probe(self, model: str, available: bool, latency_ms: float) -> None:
        await self._redis.hset(
            self._key(model),
            mapping={
                "available": "1" if available else "0",
                "latency_ms": latency_ms,
                "updated_at": time.time(),
            },
        )

    async def record_request(
        self, model: str, *, success: bool, fallback_used: bool = False
    ) -> None:
        key = self._key(model)
        data = await self._redis.hgetall(key)
        health = BackendHealth(
            available=None if data.get("available") is None else data.get("available") == "1",
            latency_ms=float(data["latency_ms"])
            if data.get("latency_ms") not in (None, "")
            else None,
            request_count=int(data.get("request_count", 0)),
            error_count=int(data.get("error_count", 0)),
            fallback_count=int(data.get("fallback_count", 0)),
            ewma_error_rate=float(data.get("ewma_error_rate", 0) or 0),
            consecutive_failures=int(data.get("consecutive_failures", 0) or 0),
            updated_at=float(data.get("updated_at", 0) or 0),
        )
        _apply_request_signal(health, success=success, fallback_used=fallback_used)
        await self._redis.hset(
            key,
            mapping={
                "request_count": health.request_count,
                "error_count": health.error_count,
                "fallback_count": health.fallback_count,
                "ewma_error_rate": health.ewma_error_rate,
                "consecutive_failures": health.consecutive_failures,
                "updated_at": health.updated_at,
            },
        )

    async def _load(self, model: str) -> BackendHealth:
        data = await self._redis.hgetall(self._key(model))
        available_raw = data.get("available")
        available = None if available_raw is None else available_raw == "1"
        latency_raw = data.get("latency_ms")
        return BackendHealth(
            available=available,
            latency_ms=float(latency_raw) if latency_raw not in (None, "") else None,
            request_count=int(data.get("request_count", 0)),
            error_count=int(data.get("error_count", 0)),
            fallback_count=int(data.get("fallback_count", 0)),
            ewma_error_rate=float(data.get("ewma_error_rate", 0) or 0),
            consecutive_failures=int(data.get("consecutive_failures", 0) or 0),
            updated_at=float(data.get("updated_at", 0) or 0),
        )

    async def ping(self) -> bool:
        return bool(await self._redis.ping())

    async def aclose(self) -> None:
        await self._redis.aclose()


def create_health_store(settings: GatewaySettings, client: httpx.AsyncClient) -> HealthStore:
    """Use Redis for fleet-wide routing state when REDIS_URL is set, else in-memory."""
    if settings.redis_url:
        from redis.asyncio import Redis

        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        return RedisHealthStore(settings, client, redis)
    return BackendHealthStore(settings, client)


async def health_probe_loop(store: HealthStore, interval_seconds: float) -> None:
    while True:
        await store.probe_all()
        await asyncio.sleep(interval_seconds)
