"""Per-tenant admission control (concurrency + bounded queue)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import HTTPException

from app.gateway.tenant_policy import TenantPolicyBundle, load_tenant_policy_bundle


@dataclass
class AdmissionLease:
    controller: TenantAdmissionController
    tenant_id: str
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self.controller._release(self.tenant_id)


class TenantAdmissionController:
    def __init__(self, bundle: TenantPolicyBundle | None = None) -> None:
        self._bundle = bundle or load_tenant_policy_bundle()
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._queued: dict[str, int] = {}
        self._inflight: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def _semaphore(self, tenant_id: str) -> asyncio.Semaphore:
        policy = self._bundle.for_tenant(tenant_id)
        if tenant_id not in self._semaphores:
            self._semaphores[tenant_id] = asyncio.Semaphore(policy.max_concurrent_requests)
        return self._semaphores[tenant_id]

    async def acquire_lease(self, tenant_id: str) -> AdmissionLease:
        policy = self._bundle.for_tenant(tenant_id)
        sem = self._semaphore(tenant_id)
        async with self._lock:
            inflight = self._inflight.get(tenant_id, 0)
            queued = self._queued.get(tenant_id, 0)
            if inflight >= policy.max_concurrent_requests and queued >= policy.max_queued_requests:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": {
                            "type": "rate_limit_error",
                            "code": "tenant_concurrency_exceeded",
                            "message": "tenant concurrent request limit exceeded",
                        }
                    },
                    headers={
                        "Retry-After": "1",
                        "X-RateLimit-Limit": str(policy.max_concurrent_requests),
                        "X-RateLimit-Remaining": "0",
                    },
                )
            self._queued[tenant_id] = queued + 1
        try:
            await sem.acquire()
        finally:
            async with self._lock:
                self._queued[tenant_id] = max(0, self._queued.get(tenant_id, 1) - 1)
                self._inflight[tenant_id] = self._inflight.get(tenant_id, 0) + 1
        return AdmissionLease(controller=self, tenant_id=tenant_id)

    async def _release(self, tenant_id: str) -> None:
        sem = self._semaphore(tenant_id)
        sem.release()
        async with self._lock:
            self._inflight[tenant_id] = max(0, self._inflight.get(tenant_id, 1) - 1)

    @asynccontextmanager
    async def acquire(self, tenant_id: str) -> AsyncIterator[AdmissionLease]:
        lease = await self.acquire_lease(tenant_id)
        try:
            yield lease
        finally:
            await lease.release()
