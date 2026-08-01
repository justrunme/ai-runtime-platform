"""Model and route catalog endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.gateway.config import GatewaySettings

router = APIRouter(tags=["models"])


@router.get("/v1/models")
async def list_models(request: Request) -> dict[str, object]:
    settings: GatewaySettings = request.app.state.settings
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model"}
            for model in [*settings.model_targets, *settings.model_routes]
        ],
    }


@router.get("/v1/routes")
async def list_routes(request: Request) -> dict[str, object]:
    """Expose public route aliases and weights without leaking backend addresses."""
    settings: GatewaySettings = request.app.state.settings
    return {
        "data": [
            {
                "id": route_name,
                "object": "model.route",
                "policy": "failover" if route.primary is not None else "weighted",
                "min_health_score": route.min_health_score,
                "unhealthy_action": route.unhealthy_action
                if route.min_health_score is not None
                else None,
                "routing_policy": route.routing_policy.model_dump()
                if route.routing_policy
                else None,
                "shadow": route.shadow,
                "targets": [target.model_dump() for target in route.targets]
                if route.primary is None
                else [
                    {"model": route.primary, "role": "primary"},
                    {"model": route.fallback, "role": "fallback"},
                ],
            }
            for route_name, route in settings.model_routes.items()
        ]
    }


@router.get("/v1/backends/health")
async def backend_health(request: Request) -> dict[str, object]:
    return {"backends": await request.app.state.backend_health.snapshot()}
