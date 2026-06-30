"""Routing decision records for replay and audit."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


@dataclass(frozen=True)
class DecisionRecord:
    request_id: str
    requested_model: str
    selected_backend: str
    routing_reason: str
    fallback_used: bool
    health_score: int | None
    duration_ms: float
    shadow_backend: str | None = None
    shadow_duration_ms: float | None = None
    shadow_outcome: str | None = None
    estimated_cost: float | None = None
    recorded_at: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def format_decision_tree(record: DecisionRecord | dict[str, object]) -> str:
    """Render a human-readable routing decision tree for the replay CLI."""
    data = record if isinstance(record, dict) else record.to_dict()
    lines = [
        f"request_id: {data['request_id']}",
        f"requested:  {data['requested_model']}",
        f"├─ selected: {data['selected_backend']}",
        f"├─ reason:   {data['routing_reason']}",
        f"├─ fallback: {data['fallback_used']}",
        f"├─ health:   {data.get('health_score', 'n/a')}",
        f"├─ latency:  {data['duration_ms']} ms",
    ]
    if data.get("estimated_cost") is not None:
        lines.append(f"├─ cost:     ${data['estimated_cost']}")
    shadow = data.get("shadow_backend")
    if shadow:
        shadow_ms = data.get("shadow_duration_ms")
        shadow_outcome = data.get("shadow_outcome", "unknown")
        lines.append(f"└─ shadow:   {shadow} ({shadow_outcome}, {shadow_ms} ms)")
    else:
        lines[-1] = lines[-1].replace("├─", "└─", 1)
    return "\n".join(lines)


class DecisionStore:
    async def put(self, record: DecisionRecord) -> None:
        raise NotImplementedError

    async def get(self, request_id: str) -> DecisionRecord | None:
        raise NotImplementedError

    async def patch_shadow(self, request_id: str, *, outcome: str, duration_ms: float) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


class InMemoryDecisionStore(DecisionStore):
    """Retain recent routing decisions on one gateway replica."""

    def __init__(self, *, max_entries: int = 1000) -> None:
        self._entries: dict[str, DecisionRecord] = {}
        self._order: list[str] = []
        self._max_entries = max_entries
        self._lock = asyncio.Lock()

    async def put(self, record: DecisionRecord) -> None:
        stamped = DecisionRecord(**{**record.to_dict(), "recorded_at": time.time()})
        async with self._lock:
            if record.request_id not in self._entries:
                self._order.append(record.request_id)
            self._entries[record.request_id] = stamped
            while len(self._order) > self._max_entries:
                oldest = self._order.pop(0)
                self._entries.pop(oldest, None)

    async def get(self, request_id: str) -> DecisionRecord | None:
        async with self._lock:
            return self._entries.get(request_id)

    async def patch_shadow(self, request_id: str, *, outcome: str, duration_ms: float) -> None:
        async with self._lock:
            current = self._entries.get(request_id)
            if current is None:
                return
            self._entries[request_id] = DecisionRecord(
                **{
                    **current.to_dict(),
                    "shadow_outcome": outcome,
                    "shadow_duration_ms": duration_ms,
                }
            )


class RedisDecisionStore(DecisionStore):
    """Share routing decisions across gateway replicas."""

    def __init__(self, redis: Redis, *, ttl_seconds: int = 3600) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _key(request_id: str) -> str:
        return f"arp:decision:{request_id}"

    async def put(self, record: DecisionRecord) -> None:
        stamped = DecisionRecord(**{**record.to_dict(), "recorded_at": time.time()})
        await self._redis.set(
            self._key(record.request_id),
            json.dumps(stamped.to_dict()),
            ex=self._ttl_seconds,
        )

    async def get(self, request_id: str) -> DecisionRecord | None:
        raw = await self._redis.get(self._key(request_id))
        if raw is None:
            return None
        data = json.loads(raw)
        return DecisionRecord(**data)

    async def patch_shadow(self, request_id: str, *, outcome: str, duration_ms: float) -> None:
        current = await self.get(request_id)
        if current is None:
            return
        updated = DecisionRecord(
            **{
                **current.to_dict(),
                "shadow_outcome": outcome,
                "shadow_duration_ms": duration_ms,
            }
        )
        await self.put(updated)

    async def aclose(self) -> None:
        await self._redis.aclose()


def create_decision_store(redis_url: str | None) -> DecisionStore:
    if redis_url:
        from redis.asyncio import Redis

        redis = Redis.from_url(redis_url, decode_responses=True)
        return RedisDecisionStore(redis)
    return InMemoryDecisionStore()
