"""Runtime status and GitOps verification endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.gateway.rbac import STATUS_ROLES, VERIFY_ROLES, require_any_role
from app.gateway.verify_signing import (
    public_jwks,
    require_signed_verify,
    sign_verification_token,
    verify_signing_enabled,
)

router = APIRouter(tags=["runtime"])


class RuntimeVerifyRequest(BaseModel):
    expected: dict[str, Any] = Field(default_factory=dict)
    remediation_id: str | None = None
    correlation_id: str | None = None


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


@router.get("/v1/runtime/jwks")
async def runtime_jwks() -> JSONResponse:
    """Public JWKS for Control Plane verification of signed verify responses."""
    return JSONResponse(public_jwks())


@router.post("/v1/runtime/verify")
async def runtime_verify(request: Request, body: RuntimeVerifyRequest) -> dict[str, Any]:
    require_any_role(request, VERIFY_ROLES)
    state = request.app.state.runtime_config
    result = state.verify(body.expected)
    if body.remediation_id:
        result["remediation_id"] = body.remediation_id
    if body.correlation_id:
        result["correlation_id"] = body.correlation_id
    token_payload = {
        "verified": result["verified"],
        "differences": result["differences"],
        "observed": result["observed"],
        "correlation": result["correlation"],
        "remediation_id": body.remediation_id,
        "correlation_id": body.correlation_id,
    }
    token = sign_verification_token(token_payload)
    if token is not None:
        result["verification_token"] = token
    elif require_signed_verify():
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "signed_verify_unavailable",
                    "message": (
                        "signed verify required but RUNTIME_VERIFY_PRIVATE_KEY is not set"
                        if not verify_signing_enabled()
                        else "failed to produce signed verification token"
                    ),
                }
            },
        )
    return result
