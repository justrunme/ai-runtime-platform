"""Tenant attribution counters for governance quota signals."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from prometheus_client import Counter

if TYPE_CHECKING:
    from redis.asyncio import Redis

TENANT_REQUESTS = Counter(
    "gateway_tenant_requests_total",
    "Chat completion requests attributed to a tenant team.",
    ["team"],
)
TENANT_TOKENS = Counter(
    "gateway_tenant_tokens_total",
    "Estimated tokens attributed to a tenant team.",
    ["team"],
)

REDIS_KEY_PREFIX = "ai:tenant:"


@dataclass
class TenantUsage:
    minute_window_start: float = field(default_factory=time.monotonic)
    requests_last_minute: int = 0
    tokens_today: int = 0


class TenantAttributionBackend(Protocol):
    def resolve_team(self, request) -> str: ...

    async def record_request(self, team: str, *, input_tokens: int, output_tokens: int) -> None: ...

    async def usage_snapshot(self, team: str) -> tuple[int, int]: ...

    async def aclose(self) -> None: ...


class TenantAttributionStore:
    """In-memory per-process counters suitable for single-replica demos."""

    def __init__(self) -> None:
        self._usage: dict[str, TenantUsage] = {}

    @staticmethod
    def enabled_from_environment() -> bool:
        return os.getenv("TENANT_ATTRIBUTION_ENABLED", "false").strip().lower() in {
            "1",
            "true",
            "yes",
        }

    def resolve_team(self, request) -> str:
        return request.headers.get("x-ai-team") or request.headers.get("x-ai-tenant") or "platform"

    def _get(self, team: str) -> TenantUsage:
        usage = self._usage.setdefault(team, TenantUsage())
        now = time.monotonic()
        if now - usage.minute_window_start >= 60:
            usage.minute_window_start = now
            usage.requests_last_minute = 0
        return usage

    async def record_request(self, team: str, *, input_tokens: int, output_tokens: int) -> None:
        usage = self._get(team)
        usage.requests_last_minute += 1
        usage.tokens_today += input_tokens + output_tokens
        TENANT_REQUESTS.labels(team=team).inc()
        TENANT_TOKENS.labels(team=team).inc(input_tokens + output_tokens)

    async def usage_snapshot(self, team: str) -> tuple[int, int]:
        usage = self._get(team)
        return usage.requests_last_minute, usage.tokens_today

    async def aclose(self) -> None:
        return None


class RedisTenantAttributionStore:
    """Fleet-wide tenant counters shared across gateway replicas."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def resolve_team(self, request) -> str:
        return request.headers.get("x-ai-team") or request.headers.get("x-ai-tenant") or "platform"

    def _key(self, team: str) -> str:
        return f"{REDIS_KEY_PREFIX}{team}"

    async def record_request(self, team: str, *, input_tokens: int, output_tokens: int) -> None:
        now = time.time()
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = self._key(team)
        pipe = self._redis.pipeline()
        pipe.hgetall(key)
        current = await pipe.execute()
        state = current[0] if current else {}

        window_start = float(state.get("window_start", 0) or 0)
        requests_last_minute = int(state.get("requests_last_minute", 0) or 0)
        tokens_today = int(state.get("tokens_today", 0) or 0)
        tokens_day = state.get("tokens_day") or ""

        if now - window_start >= 60:
            window_start = now
            requests_last_minute = 0
        if tokens_day != today:
            tokens_day = today
            tokens_today = 0

        requests_last_minute += 1
        tokens_today += input_tokens + output_tokens

        await self._redis.hset(
            key,
            mapping={
                "window_start": window_start,
                "requests_last_minute": requests_last_minute,
                "tokens_today": tokens_today,
                "tokens_day": tokens_day,
            },
        )
        TENANT_REQUESTS.labels(team=team).inc()
        TENANT_TOKENS.labels(team=team).inc(input_tokens + output_tokens)

    async def usage_snapshot(self, team: str) -> tuple[int, int]:
        state = await self._redis.hgetall(self._key(team))
        if not state:
            return 0, 0

        now = time.time()
        window_start = float(state.get("window_start", 0) or 0)
        requests_last_minute = int(state.get("requests_last_minute", 0) or 0)
        tokens_today = int(state.get("tokens_today", 0) or 0)
        tokens_day = state.get("tokens_day") or ""

        if now - window_start >= 60:
            requests_last_minute = 0
        if tokens_day != datetime.now(UTC).strftime("%Y-%m-%d"):
            tokens_today = 0
        return requests_last_minute, tokens_today

    async def aclose(self) -> None:
        await self._redis.aclose()


def create_tenant_store(redis_url: str | None) -> TenantAttributionBackend | None:
    if not TenantAttributionStore.enabled_from_environment():
        return None
    if redis_url:
        from redis.asyncio import Redis

        redis = Redis.from_url(redis_url, decode_responses=True)
        return RedisTenantAttributionStore(redis)
    return TenantAttributionStore()
