"""Runtime status and GitOps verification endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.gateway.rbac import STATUS_ROLES, VERIFY_ROLES, require_any_role

router = APIRouter(tags=["runtime"])


class RuntimeVerifyRequest(BaseModel):
    expected: dict[str, Any] = Field(default_factory=dict)


async def _backend_counts(request: Request) -> tuple[int, int, int]:
    snapshot = await request.app.state.backend_health.snapshot()
    healthy = sum(1 for row in snapshot if row.get("status") == "healthy")
    unhealthy = sum(1 for row in snapshot if row.get("status") == "unhealthy")
    unknown = sum(1 for row in snapshot if row.get("status") == "unknown")
    return healthy, unhealthy, unknown


@router.get("/v1/runtime/status")
async def runtime_status(request: Request) -> dict[str, Any]:
    require_any_role(request, STATUS_ROLES)
    state = request.app.state.runtime_config
    healthy, unhealthy, unknown = await _backend_counts(request)
    return state.status_payload(
        backends_healthy=healthy,
        backends_unhealthy=unhealthy,
        backends_unknown=unknown,
    )


@router.post("/v1/runtime/verify")
async def runtime_verify(request: Request, body: RuntimeVerifyRequest) -> dict[str, Any]:
    require_any_role(request, VERIFY_ROLES)
    state = request.app.state.runtime_config
    return state.verify(body.expected)
