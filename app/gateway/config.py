"""Gateway configuration models and environment loading."""

from __future__ import annotations

import json
import os

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.gateway.jwt_verify import is_jwt_verify_enabled


class ModelTarget(BaseModel):
    """Endpoint and unit price for one served model."""

    url: str
    input_cost_per_million: float = Field(ge=0)
    output_cost_per_million: float = Field(ge=0)
    backend_name: str | None = None
    health_path: str = "/health"
    provider: str | None = None
    model_revision: str | None = None
    model_artifact_digest: str | None = None


class RouteTarget(BaseModel):
    """One weighted model target behind a public route alias."""

    model: str
    weight: int = Field(gt=0)


class RoutingWeights(BaseModel):
    health: float = Field(default=0.5, ge=0, le=1)
    latency: float = Field(default=0.3, ge=0, le=1)
    cost: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def totals_one(self) -> RoutingWeights:
        if abs(self.health + self.latency + self.cost - 1) > 0.0001:
            raise ValueError("routing weights must total 1")
        return self


class RoutingPolicy(BaseModel):
    strategy: str = "balanced"
    weights: RoutingWeights = Field(default_factory=RoutingWeights)

    @model_validator(mode="after")
    def uses_supported_strategy(self) -> RoutingPolicy:
        if self.strategy != "balanced":
            raise ValueError("unsupported routing strategy")
        return self


class ModelRoute(BaseModel):
    """Canary or failover policy for a public model alias."""

    targets: list[RouteTarget] = Field(default_factory=list)
    primary: str | None = None
    fallback: str | None = None
    min_health_score: int | None = Field(default=None, ge=0, le=100)
    unhealthy_action: str = "skip"
    routing_policy: RoutingPolicy | None = None
    shadow: str | None = None

    @model_validator(mode="after")
    def has_valid_policy(self) -> ModelRoute:
        is_canary = bool(self.targets)
        is_failover = self.primary is not None
        if is_canary == is_failover:
            raise ValueError(
                "route must define either weighted targets or primary and fallback models"
            )
        if is_canary and sum(target.weight for target in self.targets) != 100:
            raise ValueError("route target weights must total 100")
        if is_canary and self.fallback is not None:
            raise ValueError("weighted routes cannot define a fallback model")
        if is_failover and self.fallback is None:
            raise ValueError("failover routes require a fallback model")
        if is_failover and self.primary == self.fallback:
            raise ValueError("primary and fallback models must differ")
        if self.min_health_score is not None and not is_failover:
            raise ValueError("health-aware routing requires a primary and fallback policy")
        if self.min_health_score is not None and self.unhealthy_action != "skip":
            raise ValueError("unsupported unhealthy action")
        if self.routing_policy is not None and not is_failover:
            raise ValueError("cost-aware routing requires a primary and fallback policy")
        if self.shadow is not None and self.shadow not in self.model_names():
            raise ValueError("shadow must reference a model configured on the route")
        return self

    def model_names(self) -> list[str]:
        if self.primary is not None:
            return [self.primary, self.fallback]  # type: ignore[list-item]
        return [target.model for target in self.targets]


class GatewaySettings(BaseModel):
    """Runtime configuration. MODEL_TARGETS is intentionally JSON for GitOps injection."""

    model_config = ConfigDict(frozen=True)
    model_targets: dict[str, ModelTarget]
    model_routes: dict[str, ModelRoute] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=120.0, gt=0)
    health_interval_seconds: float = Field(default=15.0, gt=0)
    api_keys: frozenset[str] = Field(default_factory=frozenset)
    redis_url: str | None = None
    gateway_replicas: int = Field(default=1, ge=1)
    require_shared_state: bool = False
    profile: str = "local"
    jwt_verify_enabled: bool = False
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    require_jwt_iss_aud: bool = False
    control_plane_configured: bool = False
    require_auth: bool = False
    require_control_plane: bool = False

    @model_validator(mode="after")
    def route_targets_exist(self) -> GatewaySettings:
        missing = {
            model
            for route in self.model_routes.values()
            for model in route.model_names()
            if model not in self.model_targets
        }
        if missing:
            raise ValueError(f"route references unknown models: {', '.join(sorted(missing))}")
        return self

    @model_validator(mode="after")
    def shared_state_required_for_ha(self) -> GatewaySettings:
        if self.require_shared_state and not self.redis_url:
            raise ValueError(
                "REDIS_URL is required when REQUIRE_SHARED_STATE=true or GATEWAY_REPLICAS > 1"
            )
        return self

    @model_validator(mode="after")
    def profile_security_guards(self) -> GatewaySettings:
        if self.profile not in {"local", "internal", "production"}:
            raise ValueError("GATEWAY_PROFILE must be one of: local, internal, production")
        has_auth = bool(self.api_keys) or self.jwt_verify_enabled
        if self.require_auth and not has_auth:
            raise ValueError(
                "authentication is required: set GATEWAY_API_KEYS and/or OIDC_JWT_VERIFY=true"
            )
        if self.require_control_plane and not self.control_plane_configured:
            raise ValueError("CONTROL_PLANE_URL is required for this GATEWAY_PROFILE")
        if self.profile == "production":
            if not self.jwt_verify_enabled:
                raise ValueError(
                    "production profile requires OIDC_JWT_VERIFY=true and a configured JWKS URL"
                )
            if not self.jwt_issuer or not self.jwt_audience:
                raise ValueError(
                    "production profile requires OIDC_JWT_ISSUER and OIDC_JWT_AUDIENCE"
                )
            if not self.require_jwt_iss_aud:
                raise ValueError("production profile requires OIDC_JWT_REQUIRE_ISS_AUD=true")
            if not self.require_shared_state or not self.redis_url:
                raise ValueError("production profile requires REDIS_URL shared state")
            if not self.control_plane_configured:
                raise ValueError("production profile requires CONTROL_PLANE_URL")
        return self

    @classmethod
    def from_environment(cls) -> GatewaySettings:
        default_targets = {
            "qwen2.5-7b-instruct": {
                "url": "http://vllm-qwen.ai-runtime.svc.cluster.local:8000",
                "input_cost_per_million": 0.20,
                "output_cost_per_million": 0.20,
            }
        }
        raw_targets = os.getenv("MODEL_TARGETS")
        model_targets = json.loads(raw_targets) if raw_targets else default_targets
        ollama_base_url = os.getenv("OLLAMA_BASE_URL")
        if ollama_base_url:
            ollama_models = os.getenv("OLLAMA_MODELS", os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b"))
            ollama_url = ollama_base_url.rstrip("/")
            if not ollama_url.endswith("/v1"):
                ollama_url = f"{ollama_url}/v1"
            for ollama_model in (
                model.strip() for model in ollama_models.split(",") if model.strip()
            ):
                model_targets.setdefault(
                    ollama_model,
                    {
                        "url": ollama_url,
                        "input_cost_per_million": float(
                            os.getenv("OLLAMA_INPUT_COST_PER_MILLION", "0")
                        ),
                        "output_cost_per_million": float(
                            os.getenv("OLLAMA_OUTPUT_COST_PER_MILLION", "0")
                        ),
                        "backend_name": f"ollama-{ollama_model}",
                        "health_path": "/",
                    },
                )
        raw_routes = os.getenv("MODEL_ROUTES")
        raw_api_keys = os.getenv("GATEWAY_API_KEYS", "")
        api_keys = frozenset(key.strip() for key in raw_api_keys.split(",") if key.strip())
        gateway_replicas = max(1, int(os.getenv("GATEWAY_REPLICAS", "1")))
        profile = os.getenv("GATEWAY_PROFILE", "local").strip().lower() or "local"
        jwt_verify_enabled = is_jwt_verify_enabled()
        jwt_issuer = os.getenv("OIDC_JWT_ISSUER", "").strip() or None
        jwt_audience = os.getenv("OIDC_JWT_AUDIENCE", "").strip() or None
        require_jwt_iss_aud = profile == "production" or os.getenv(
            "OIDC_JWT_REQUIRE_ISS_AUD", ""
        ).strip().lower() in {"1", "true", "yes"}
        control_plane_configured = bool(os.getenv("CONTROL_PLANE_URL", "").strip())
        require_shared_state = (
            os.getenv("REQUIRE_SHARED_STATE", "").strip().lower()
            in {
                "1",
                "true",
                "yes",
            }
            or gateway_replicas > 1
            or profile == "production"
        )
        require_auth = profile in {"internal", "production"} or os.getenv(
            "REQUIRE_AUTH", ""
        ).strip().lower() in {"1", "true", "yes"}
        require_control_plane = profile == "production" or os.getenv(
            "REQUIRE_CONTROL_PLANE", ""
        ).strip().lower() in {"1", "true", "yes"}
        return cls.model_validate(
            {
                "model_targets": model_targets,
                "model_routes": json.loads(raw_routes) if raw_routes else {},
                "timeout_seconds": float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "120")),
                "health_interval_seconds": float(
                    os.getenv("BACKEND_HEALTH_INTERVAL_SECONDS", "15")
                ),
                "api_keys": api_keys,
                "redis_url": os.getenv("REDIS_URL") or None,
                "gateway_replicas": gateway_replicas,
                "require_shared_state": require_shared_state,
                "profile": profile,
                "jwt_verify_enabled": jwt_verify_enabled,
                "jwt_issuer": jwt_issuer,
                "jwt_audience": jwt_audience,
                "require_jwt_iss_aud": require_jwt_iss_aud,
                "control_plane_configured": control_plane_configured,
                "require_auth": require_auth,
                "require_control_plane": require_control_plane,
            }
        )
