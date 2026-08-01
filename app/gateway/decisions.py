"""Routing decision records for replay and audit."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis.asyncio import Redis


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value in (None, "", "None"):
        return None
    return str(value)


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
    stream_outcome: str | None = None
    stream_ttft_ms: float | None = None
    # Enforcement evidence (Control Plane correlation). Absent on pre-1.4 records.
    control_plane_decision_id: str | None = None
    approval_id: str | None = None
    policy_bundle_id: str | None = None
    policy_digest: str | None = None
    request_digest: str | None = None
    control_plane_version: str | None = None
    runtime_version: str | None = None
    enforcement_outcome: str | None = None
    # Tenant isolation (v1.6). Absent on pre-1.6 records → treated as shared/legacy.
    tenant_id: str | None = None
    recorded_at: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> DecisionRecord:
        return cls(
            request_id=str(data["request_id"]),
            requested_model=str(data["requested_model"]),
            selected_backend=str(data["selected_backend"]),
            routing_reason=str(data["routing_reason"]),
            fallback_used=str(data.get("fallback_used", "false")).lower() in {"1", "true", "yes"}
            if not isinstance(data.get("fallback_used"), bool)
            else bool(data.get("fallback_used")),
            health_score=(
                None
                if data.get("health_score") in (None, "", "None")
                else int(float(data["health_score"]))
            ),
            duration_ms=float(data.get("duration_ms") or 0),
            shadow_backend=data.get("shadow_backend") or None,
            shadow_duration_ms=(
                None
                if data.get("shadow_duration_ms") in (None, "", "None")
                else float(data["shadow_duration_ms"])
            ),
            shadow_outcome=data.get("shadow_outcome") or None,
            estimated_cost=(
                None
                if data.get("estimated_cost") in (None, "", "None")
                else float(data["estimated_cost"])
            ),
            stream_outcome=data.get("stream_outcome") or None,
            stream_ttft_ms=(
                None
                if data.get("stream_ttft_ms") in (None, "", "None")
                else float(data["stream_ttft_ms"])
            ),
            control_plane_decision_id=_optional_str(data, "control_plane_decision_id"),
            approval_id=_optional_str(data, "approval_id"),
            policy_bundle_id=_optional_str(data, "policy_bundle_id"),
            policy_digest=_optional_str(data, "policy_digest"),
            request_digest=_optional_str(data, "request_digest"),
            control_plane_version=_optional_str(data, "control_plane_version"),
            runtime_version=_optional_str(data, "runtime_version"),
            enforcement_outcome=_optional_str(data, "enforcement_outcome"),
            tenant_id=_optional_str(data, "tenant_id"),
            recorded_at=float(data.get("recorded_at") or 0),
        )


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

    async def get(
        self,
        request_id: str,
        *,
        tenant_id: str | None = None,
        any_tenant: bool = False,
    ) -> DecisionRecord | None:
        raise NotImplementedError

    async def patch_shadow(self, request_id: str, *, outcome: str, duration_ms: float) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None

    async def ping(self) -> bool:
        return True


class InMemoryDecisionStore(DecisionStore):
    """Retain recent routing decisions on one gateway replica."""

    def __init__(self, *, max_entries: int = 1000) -> None:
        self._entries: dict[str, DecisionRecord] = {}
        self._order: list[str] = []
        self._max_entries = max_entries
        self._lock = asyncio.Lock()

    @staticmethod
    def _storage_key(record: DecisionRecord) -> str:
        tenant = record.tenant_id or "platform"
        return f"{tenant}:{record.request_id}"

    async def put(self, record: DecisionRecord) -> None:
        stamped = DecisionRecord(**{**record.to_dict(), "recorded_at": time.time()})
        key = self._storage_key(stamped)
        async with self._lock:
            if key not in self._entries:
                self._order.append(key)
            self._entries[key] = stamped
            while len(self._order) > self._max_entries:
                oldest = self._order.pop(0)
                self._entries.pop(oldest, None)

    async def get(
        self,
        request_id: str,
        *,
        tenant_id: str | None = None,
        any_tenant: bool = False,
    ) -> DecisionRecord | None:
        async with self._lock:
            if any_tenant:
                for record in self._entries.values():
                    if record.request_id == request_id:
                        return record
                return None
            tenant = tenant_id or "platform"
            record = self._entries.get(f"{tenant}:{request_id}")
            if record is not None:
                return record
            # Legacy unscoped records (pre-1.6 tests / single-tenant demos).
            return self._entries.get(request_id)

    async def patch_shadow(self, request_id: str, *, outcome: str, duration_ms: float) -> None:
        async with self._lock:
            current = None
            key = None
            for storage_key, record in self._entries.items():
                if record.request_id == request_id:
                    current = record
                    key = storage_key
                    break
            if current is None or key is None:
                return
            self._entries[key] = DecisionRecord(
                **{
                    **current.to_dict(),
                    "shadow_outcome": outcome,
                    "shadow_duration_ms": duration_ms,
                }
            )


class RedisDecisionStore(DecisionStore):
    """Share routing decisions across gateway replicas using Redis hashes."""

    def __init__(self, redis: Redis, *, ttl_seconds: int = 3600) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _key(tenant_id: str, request_id: str) -> str:
        return f"arp:{tenant_id}:decision:{request_id}"

    @staticmethod
    def _legacy_key(request_id: str) -> str:
        return f"arp:decision:{request_id}"

    @staticmethod
    def _index_key(request_id: str) -> str:
        return f"arp:decision-index:{request_id}"

    @staticmethod
    def _serialize(record: DecisionRecord) -> dict[str, str]:
        payload: dict[str, str] = {}
        for key, value in record.to_dict().items():
            if value is None:
                payload[key] = ""
            elif isinstance(value, bool):
                payload[key] = "true" if value else "false"
            else:
                payload[key] = str(value)
        return payload

    def _decode(self, data: dict[str, Any]) -> DecisionRecord | None:
        if not data:
            return None
        if len(data) == 1 and "data" in data:
            return DecisionRecord(**json.loads(data["data"]))
        return DecisionRecord.from_mapping(data)

    async def put(self, record: DecisionRecord) -> None:
        stamped = DecisionRecord(**{**record.to_dict(), "recorded_at": time.time()})
        tenant = stamped.tenant_id or "platform"
        key = self._key(tenant, stamped.request_id)
        pipe = self._redis.pipeline()
        pipe.hset(key, mapping=self._serialize(stamped))
        pipe.expire(key, self._ttl_seconds)
        pipe.set(self._index_key(stamped.request_id), tenant, ex=self._ttl_seconds)
        await pipe.execute()

    async def get(
        self,
        request_id: str,
        *,
        tenant_id: str | None = None,
        any_tenant: bool = False,
    ) -> DecisionRecord | None:
        if any_tenant:
            indexed = await self._redis.get(self._index_key(request_id))
            if indexed:
                data = await self._redis.hgetall(self._key(str(indexed), request_id))
                decoded = self._decode(data)
                if decoded is not None:
                    return decoded
            return self._decode(await self._redis.hgetall(self._legacy_key(request_id)))

        tenant = tenant_id or "platform"
        data = await self._redis.hgetall(self._key(tenant, request_id))
        decoded = self._decode(data)
        if decoded is not None:
            return decoded
        return self._decode(await self._redis.hgetall(self._legacy_key(request_id)))

    async def patch_shadow(self, request_id: str, *, outcome: str, duration_ms: float) -> None:
        indexed = await self._redis.get(self._index_key(request_id))
        key = self._key(str(indexed), request_id) if indexed else self._legacy_key(request_id)
        # Atomic field update — avoids lost updates from concurrent read-modify-write.
        updated = await self._redis.hset(
            key,
            mapping={
                "shadow_outcome": outcome,
                "shadow_duration_ms": str(duration_ms),
            },
        )
        if updated == 0 and not await self._redis.exists(key):
            return
        await self._redis.expire(key, self._ttl_seconds)

    async def ping(self) -> bool:
        return bool(await self._redis.ping())

    async def aclose(self) -> None:
        await self._redis.aclose()


def create_decision_store(redis_url: str | None) -> DecisionStore:
    if redis_url:
        from redis.asyncio import Redis

        redis = Redis.from_url(redis_url, decode_responses=True)
        return RedisDecisionStore(redis)
    return InMemoryDecisionStore()
