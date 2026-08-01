"""Routing decision lookup endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.gateway.errors import raise_api_error
from app.gateway.tenant_context import is_global_auditor, resolve_tenant_id

router = APIRouter(tags=["decisions"])


@router.get("/v1/decisions/{request_id}")
async def get_decision(request: Request, request_id: str) -> dict[str, object]:
    tenant_id = resolve_tenant_id(request)
    auditor = is_global_auditor(request)
    record = await request.app.state.decision_store.get(
        request_id,
        tenant_id=tenant_id,
        any_tenant=auditor,
    )
    if record is None:
        raise_api_error(
            404,
            code="decision_not_found",
            message="routing decision not found",
            error_type="invalid_request_error",
        )
    if record.tenant_id and record.tenant_id != tenant_id and not auditor:
        # Do not leak cross-tenant existence.
        raise_api_error(
            404,
            code="decision_not_found",
            message="routing decision not found",
            error_type="invalid_request_error",
        )
    return record.to_dict()
