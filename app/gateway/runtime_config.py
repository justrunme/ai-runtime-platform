"""Immutable runtime configuration snapshots for GitOps verification."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from app.gateway.config import GatewaySettings
from app.gateway.evidence import runtime_version


def _canonical_config_payload(settings: GatewaySettings) -> dict[str, Any]:
    """Serialize non-secret settings that define execution-plane behavior."""
    return {
        "model_targets": {
            name: target.model_dump(mode="json")
            for name, target in sorted(settings.model_targets.items())
        },
        "model_routes": {
            name: route.model_dump(mode="json")
            for name, route in sorted(settings.model_routes.items())
        },
        "timeout_seconds": settings.timeout_seconds,
        "health_interval_seconds": settings.health_interval_seconds,
        "profile": settings.profile,
        "gateway_replicas": settings.gateway_replicas,
        "require_shared_state": settings.require_shared_state,
        "control_plane_configured": settings.control_plane_configured,
        "jwt_verify_enabled": settings.jwt_verify_enabled,
        "jwt_issuer": settings.jwt_issuer,
        "jwt_audience": settings.jwt_audience,
    }


def compute_config_digest(settings: GatewaySettings) -> str:
    payload = json.dumps(_canonical_config_payload(settings), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def compute_routes_digest(settings: GatewaySettings) -> str:
    routes = {
        name: route.model_dump(mode="json") for name, route in sorted(settings.model_routes.items())
    }
    models = sorted(settings.model_targets)
    payload = json.dumps(
        {"routes": routes, "models": models}, sort_keys=True, separators=(",", ":")
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


@dataclass
class PolicyObservation:
    last_seen_bundle_id: str | None = None
    last_seen_digest: str | None = None
    updated_at: float | None = None

    def observe(self, *, bundle_id: str | None, digest: str | None) -> None:
        if bundle_id:
            self.last_seen_bundle_id = bundle_id
        if digest:
            self.last_seen_digest = digest
        if bundle_id or digest:
            self.updated_at = time.time()


@dataclass(frozen=True)
class RuntimeConfigSnapshot:
    """Immutable view of the configuration this process actually loaded."""

    digest: str
    generation: int
    loaded_at: float
    profile: str
    models: tuple[str, ...]
    routes_digest: str
    status: str = "active"  # active | last_known_good | rejected

    def as_dict(self) -> dict[str, Any]:
        return {
            "observed_digest": self.digest,
            "generation": self.generation,
            "loaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.loaded_at)),
            "profile": self.profile,
            "models": list(self.models),
            "routes_digest": self.routes_digest,
            "status": self.status,
        }


@dataclass
class RuntimeConfigState:
    """Active snapshot plus last-known-good / rejected markers for remediation evidence."""

    active: RuntimeConfigSnapshot
    last_known_good: RuntimeConfigSnapshot
    rejected: RuntimeConfigSnapshot | None = None
    policy: PolicyObservation = field(default_factory=PolicyObservation)
    instance_id: str = field(
        default_factory=lambda: (
            os.getenv("GATEWAY_INSTANCE_ID") or os.getenv("HOSTNAME") or "gateway"
        )
    )

    @classmethod
    def from_settings(cls, settings: GatewaySettings) -> RuntimeConfigState:
        generation = int(os.getenv("GATEWAY_CONFIG_GENERATION", "1") or "1")
        snapshot = RuntimeConfigSnapshot(
            digest=compute_config_digest(settings),
            generation=generation,
            loaded_at=time.time(),
            profile=settings.profile,
            models=tuple(sorted(settings.model_targets)),
            routes_digest=compute_routes_digest(settings),
            status="active",
        )
        return cls(active=snapshot, last_known_good=snapshot)

    def status_payload(
        self,
        *,
        backends_healthy: int,
        backends_unhealthy: int,
        backends_unknown: int,
    ) -> dict[str, Any]:
        return {
            "runtime_version": runtime_version(),
            "instance_id": self.instance_id,
            "configuration": self.active.as_dict(),
            "last_known_good": {
                "observed_digest": self.last_known_good.digest,
                "generation": self.last_known_good.generation,
            },
            "rejected": None
            if self.rejected is None
            else {
                "observed_digest": self.rejected.digest,
                "generation": self.rejected.generation,
                "status": "rejected",
            },
            "policy": {
                "last_seen_bundle_id": self.policy.last_seen_bundle_id,
                "last_seen_digest": self.policy.last_seen_digest,
            },
            "routes": {
                "digest": self.active.routes_digest,
                "models": list(self.active.models),
            },
            "backends": {
                "healthy": backends_healthy,
                "unhealthy": backends_unhealthy,
                "unknown": backends_unknown,
            },
        }

    def verify(self, expected: dict[str, Any]) -> dict[str, Any]:
        differences: list[dict[str, Any]] = []
        checks = [
            ("config_digest", expected.get("config_digest"), self.active.digest),
            ("generation", expected.get("generation"), self.active.generation),
            ("policy_digest", expected.get("policy_digest"), self.policy.last_seen_digest),
            ("routes_digest", expected.get("routes_digest"), self.active.routes_digest),
        ]
        for field_name, want, actual in checks:
            if want is None or want == "":
                continue
            if want != actual:
                differences.append({"field": field_name, "expected": want, "actual": actual})
        models = expected.get("models")
        if isinstance(models, list):
            actual_models = list(self.active.models)
            if sorted(str(m) for m in models) != actual_models:
                differences.append({"field": "models", "expected": models, "actual": actual_models})
        return {"verified": not differences, "differences": differences}
