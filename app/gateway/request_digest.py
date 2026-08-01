"""Canonical request digest shared with Control Plane approval binding."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

# Keep in sync with ai-infra-control-plane approval_binding._BINDING_FIELDS.
# Live telemetry (requests_last_minute / tokens_today) is intentionally excluded.
_BINDING_FIELDS: tuple[str, ...] = (
    "subject",
    "groups",
    "policy_pack",
    "team",
    "tenant_id",
    "owner",
    "environment",
    "namespace",
    "action",
    "model",
    "provider",
    "input_tokens",
    "output_tokens",
    "cost_per_request_usd",
    "cost_per_hour_usd",
    "month_to_date_cost_usd",
    "forecast_monthly_cost_usd",
    "sensitive_data",
    "tool_access",
    "write_permission",
    "model_revision",
    "model_artifact_digest",
    "agent",
    "region",
)


def binding_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return the normalized subset of fields used for approval/decision binding."""
    payload: dict[str, Any] = {}
    for key in _BINDING_FIELDS:
        value = request.get(key)
        if key == "groups":
            payload[key] = sorted(str(item) for item in (value or []))
        elif key == "tenant_id":
            payload[key] = str(value or request.get("team") or "")
        elif isinstance(value, bool):
            payload[key] = value
        elif value is None:
            payload[key] = ""
        else:
            payload[key] = value
    return payload


def compute_request_digest(request: Mapping[str, Any]) -> str:
    """SHA-256 digest of the canonical approval-binding payload (no sha256: prefix)."""
    canonical = json.dumps(
        binding_payload(request),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
