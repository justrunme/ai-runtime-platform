"""Admission counters must not leak on cancellation while waiting."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.gateway.admission import TenantAdmissionController
from app.gateway.resilience import GlobalAdmissionController
from app.gateway.tenant_policy import TenantPolicyBundle, TenantRuntimePolicy


@pytest.mark.anyio
async def test_tenant_admission_cancel_while_waiting_does_not_leak() -> None:
    bundle = TenantPolicyBundle(
        tenants={
            "finance": TenantRuntimePolicy(
                maxConcurrentRequests=1,
                maxQueuedRequests=10,
            )
        }
    )
    controller = TenantAdmissionController(bundle)
    first = await controller.acquire_lease("finance")
    waiter = asyncio.create_task(controller.acquire_lease("finance"))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await first.release()
    assert controller._queued.get("finance", 0) == 0
    assert controller._inflight.get("finance", 0) == 0
    third = await controller.acquire_lease("finance")
    await third.release()


@pytest.mark.anyio
async def test_global_admission_cancel_while_waiting_does_not_leak() -> None:
    controller = GlobalAdmissionController(max_inflight=1, max_queued=10)
    await controller.acquire()
    waiter = asyncio.create_task(controller.acquire())
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await controller.release()
    assert controller._queued == 0
    assert controller._inflight == 0
    await controller.acquire()
    await controller.release()


@pytest.mark.anyio
async def test_tenant_admission_still_rejects_when_saturated() -> None:
    bundle = TenantPolicyBundle(
        tenants={
            "finance": TenantRuntimePolicy(
                maxConcurrentRequests=1,
                maxQueuedRequests=0,
            )
        }
    )
    controller = TenantAdmissionController(bundle)
    first = await controller.acquire_lease("finance")
    with pytest.raises(HTTPException) as error:
        await controller.acquire_lease("finance")
    assert error.value.status_code == 429
    await first.release()
