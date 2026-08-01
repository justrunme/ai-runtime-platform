"""Liveness / readiness checks for the Runtime Gateway."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI

from app.gateway.governance import GovernanceConfig


async def build_readiness_report(app: FastAPI) -> tuple[bool, dict[str, Any]]:
    settings = getattr(app.state, "settings", None)
    checks: dict[str, Any] = {}

    route_ok = bool(settings and settings.model_targets)
    checks["route_registry"] = {
        "ok": route_ok,
        "models": len(settings.model_targets) if settings else 0,
    }

    health_store = getattr(app.state, "backend_health", None)
    decision_store = getattr(app.state, "decision_store", None)
    redis_required = bool(settings and settings.require_shared_state)
    redis_configured = bool(settings and settings.redis_url)
    redis_ok = True
    if redis_configured:
        try:
            redis_ok = bool(decision_store and await decision_store.ping())
            if health_store is not None and hasattr(health_store, "ping"):
                redis_ok = redis_ok and bool(await health_store.ping())
        except Exception as error:  # noqa: BLE001 - readiness must stay resilient
            redis_ok = False
            checks["redis_error"] = str(error)
    elif redis_required:
        redis_ok = False
    checks["redis"] = {
        "ok": redis_ok,
        "required": redis_required,
        "configured": redis_configured,
    }

    governance: GovernanceConfig | None = getattr(app.state, "governance", None)
    control_plane_ok = True
    control_plane_required = bool(governance and governance.enabled and not governance.fail_open)
    if control_plane_required and governance is not None:
        client: httpx.AsyncClient | None = getattr(app.state, "client", None)
        try:
            if client is None:
                control_plane_ok = False
            else:
                response = await client.get(
                    f"{governance.control_plane_url}/readyz",
                    timeout=min(2.0, governance.timeout_seconds),
                )
                if response.status_code == 404:
                    response = await client.get(
                        f"{governance.control_plane_url}/healthz",
                        timeout=min(2.0, governance.timeout_seconds),
                    )
                control_plane_ok = response.is_success
        except Exception as error:  # noqa: BLE001
            control_plane_ok = False
            checks["control_plane_error"] = str(error)
    checks["control_plane"] = {
        "ok": control_plane_ok,
        "required": control_plane_required,
        "url": governance.control_plane_url if governance else None,
    }

    backends: list[dict[str, Any]] = []
    available_routes = 0
    if health_store is not None and settings is not None:
        try:
            for row in await health_store.snapshot():
                backends.append(row)
                if row.get("status") != "unhealthy":
                    available_routes += 1
        except Exception as error:  # noqa: BLE001 - readiness must not 500
            checks["backends_error"] = str(error)
            if redis_configured or redis_required:
                redis_ok = False
                checks["redis"] = {
                    "ok": False,
                    "required": redis_required,
                    "configured": redis_configured,
                }
    checks["backends"] = {
        "available_routes": available_routes,
        "total": len(backends),
        "items": backends,
    }

    drain = getattr(app.state, "drain", None)
    draining = bool(drain and drain.draining)
    checks["drain"] = {"ok": not draining, "draining": draining}

    ready = (
        route_ok
        and redis_ok
        and control_plane_ok
        and not draining
        and (available_routes > 0 or not backends)
    )
    status = "ready" if ready else "not_ready"
    if draining:
        status = "draining"
    elif ready and backends and available_routes < len(backends):
        status = "degraded"
    return ready, {"status": status, "checks": checks}
