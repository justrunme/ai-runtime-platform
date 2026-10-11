"""Liveness and readiness routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.gateway.readiness import build_readiness_report

router = APIRouter(tags=["health"])


def _governance_mode(request: Request) -> str:
    governance = getattr(request.app.state, "governance", None)
    if governance is None:
        return "disabled"
    return str(getattr(governance, "mode", "disabled"))


@router.get("/livez")
async def livez() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health")
@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    return {"status": "ok", "governance_mode": _governance_mode(request)}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    ready, report = await build_readiness_report(request.app)
    report["governance_mode"] = _governance_mode(request)
    return JSONResponse(status_code=200 if ready else 503, content=report)
