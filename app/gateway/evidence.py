"""Enforcement evidence: correlate Control Plane decisions with Runtime execution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any


def runtime_version() -> str:
    """Prefer package metadata; fall back to FastAPI app version env override."""
    override = os.getenv("GATEWAY_RUNTIME_VERSION", "").strip()
    if override:
        return override
    try:
        return package_version("ai-runtime-platform")
    except PackageNotFoundError:
        return "0.0.0-dev"


@dataclass(frozen=True)
class EnforcementEvidence:
    """Provenance linking a client request to a Control Plane governance decision."""

    control_plane_decision_id: str | None = None
    approval_id: str | None = None
    policy_bundle_id: str | None = None
    policy_digest: str | None = None
    request_digest: str | None = None
    control_plane_version: str | None = None
    runtime_version: str | None = None
    enforcement_outcome: str | None = None

    def as_decision_fields(self) -> dict[str, str | None]:
        """Fields safe to splat into record_decision (outcome passed separately)."""
        return {
            "control_plane_decision_id": self.control_plane_decision_id,
            "approval_id": self.approval_id,
            "policy_bundle_id": self.policy_bundle_id,
            "policy_digest": self.policy_digest,
            "request_digest": self.request_digest,
            "control_plane_version": self.control_plane_version,
            "runtime_version": self.runtime_version or runtime_version(),
        }


def evidence_from_governance(
    result: Mapping[str, Any] | None,
    *,
    enforcement_outcome: str,
) -> EnforcementEvidence:
    if not result:
        return EnforcementEvidence(
            runtime_version=runtime_version(),
            enforcement_outcome=enforcement_outcome,
        )
    decision_id = result.get("decision_id") or result.get("control_plane_decision_id")
    return EnforcementEvidence(
        control_plane_decision_id=str(decision_id) if decision_id else None,
        approval_id=str(result["approval_id"]) if result.get("approval_id") else None,
        policy_bundle_id=str(result["policy_bundle_id"])
        if result.get("policy_bundle_id")
        else None,
        policy_digest=str(result["policy_digest"]) if result.get("policy_digest") else None,
        request_digest=str(result["request_digest"]) if result.get("request_digest") else None,
        control_plane_version=(
            str(result["control_plane_version"]) if result.get("control_plane_version") else None
        ),
        runtime_version=runtime_version(),
        enforcement_outcome=enforcement_outcome,
    )


def apply_evidence_headers(
    headers: dict[str, str],
    evidence: EnforcementEvidence | None,
) -> dict[str, str]:
    """Attach enforcement evidence headers without mutating the OpenAI response body."""
    if evidence is None:
        headers["x-ai-runtime-version"] = runtime_version()
        return headers
    headers["x-ai-runtime-version"] = evidence.runtime_version or runtime_version()
    if evidence.control_plane_decision_id:
        headers["x-ai-control-decision-id"] = evidence.control_plane_decision_id
        # Backward-compatible alias used since v1.2 governance responses.
        headers["x-ai-decision-id"] = evidence.control_plane_decision_id
    if evidence.policy_bundle_id:
        headers["x-ai-policy-bundle-id"] = evidence.policy_bundle_id
    if evidence.policy_digest:
        headers["x-ai-policy-digest"] = evidence.policy_digest
    if evidence.request_digest:
        headers["x-ai-request-digest"] = evidence.request_digest
    if evidence.approval_id:
        headers["x-ai-approval-id"] = evidence.approval_id
    return headers
