"""Liveness and readiness routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.gateway.readiness import build_readiness_report

router = APIRouter(tags=["health"])


@router.get("/livez")
async def livez() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return await livez()


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    ready, report = await build_readiness_report(request.app)
    return JSONResponse(status_code=200 if ready else 503, content=report)
