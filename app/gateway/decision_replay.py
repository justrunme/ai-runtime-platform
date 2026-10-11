"""Single-use decision-token (jti) replay cache."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from redis.asyncio import Redis

REDIS_KEY_PREFIX = "arp:decision:jti:"


def replay_protection_enabled() -> bool:
    """Replay protection is on by default when a decision token is present."""
    raw = os.getenv("GATEWAY_DECISION_REPLAY_PROTECTION", "true").strip().lower()
    return raw in {"1", "true", "yes"}


def derive_jti(*, claims: dict, token: str) -> str:
    """Prefer explicit jti, then decision_id, then a stable hash of the token."""
    for key in ("jti", "decision_id"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def binding_value(*, tenant: str, request_digest: str) -> str:
    return f"{tenant}|{request_digest}"


class DecisionReplayBackend(Protocol):
    async def consume(
        self,
        jti: str,
        *,
        tenant: str,
        request_digest: str,
        ttl_seconds: int,
    ) -> bool:
        """Atomically mark jti as used. Returns False if already consumed."""
        ...

    async def aclose(self) -> None: ...


@dataclass
class InMemoryDecisionReplayStore:
    """Process-local replay cache for demos without Redis."""

    _entries: dict[str, tuple[str, float]] = field(default_factory=dict)

    async def consume(
        self,
        jti: str,
        *,
        tenant: str,
        request_digest: str,
        ttl_seconds: int,
    ) -> bool:
        now = time.monotonic()
        expired = [key for key, (_, exp) in self._entries.items() if exp <= now]
        for key in expired:
            self._entries.pop(key, None)
        binding = binding_value(tenant=tenant, request_digest=request_digest)
        existing = self._entries.get(jti)
        if existing is not None:
            return False
        self._entries[jti] = (binding, now + max(1, ttl_seconds))
        return True

    async def aclose(self) -> None:
        return None


class RedisDecisionReplayStore:
    """Fleet-wide single-use jti store (SET NX EX)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def _key(self, jti: str) -> str:
        return f"{REDIS_KEY_PREFIX}{jti}"

    async def consume(
        self,
        jti: str,
        *,
        tenant: str,
        request_digest: str,
        ttl_seconds: int,
    ) -> bool:
        binding = binding_value(tenant=tenant, request_digest=request_digest)
        # SET NX EX — only the first consumer wins across replicas.
        created = await self._redis.set(
            self._key(jti),
            binding,
            nx=True,
            ex=max(1, ttl_seconds),
        )
        return bool(created)

    async def aclose(self) -> None:
        await self._redis.aclose()


def create_decision_replay_store(redis_url: str | None) -> DecisionReplayBackend | None:
    if not replay_protection_enabled():
        return None
    if redis_url:
        from redis.asyncio import Redis

        redis = Redis.from_url(redis_url, decode_responses=True)
        return RedisDecisionReplayStore(redis)
    return InMemoryDecisionReplayStore()


def verify_token_binding(
    *,
    token_tenant: str,
    token_digest: str,
    expected_tenant: str,
    expected_digest: str,
) -> bool:
    """Constant-time check that the token is bound to this request/tenant."""
    tenant_ok = (not token_tenant or not expected_tenant) or hmac.compare_digest(
        token_tenant, expected_tenant
    )
    digest_ok = (not token_digest or not expected_digest) or hmac.compare_digest(
        token_digest, expected_digest
    )
    return tenant_ok and digest_ok
