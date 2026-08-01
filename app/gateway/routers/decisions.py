"""Routing decision lookup endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.gateway.errors import raise_api_error

router = APIRouter(tags=["decisions"])


@router.get("/v1/decisions/{request_id}")
async def get_decision(request: Request, request_id: str) -> dict[str, object]:
    record = await request.app.state.decision_store.get(request_id)
    if record is None:
        raise_api_error(
            404,
            code="decision_not_found",
            message="routing decision not found",
            error_type="invalid_request_error",
        )
    return record.to_dict()
