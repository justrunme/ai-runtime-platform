"""Evidence envelopes for budget-guard decisions (ASC-shaped, decision ALLOW/DENY).

Adapted from https://github.com/justrunme/agent-security-contract evidence envelope
schema 1.0.0: schema_version, policy_version, policy_sha256, trace_id, commit_sha,
timestamp, envelope_sha256. Uses ``decision`` (ALLOW/DENY) as required by this
runtime guard; ``verdict`` / ``acs_disposition`` are included for ASC alignment.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.gateway.budget_guard import BudgetDecision

SCHEMA_VERSION = "1.0.0"
DIGEST_FIELD = "envelope_sha256"


def resolve_commit_sha(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for key in ("GITHUB_SHA", "COMMIT_SHA", "GIT_COMMIT"):
        value = os.environ.get(key)
        if value:
            return value
    return "unknown"


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def envelope_digest(envelope: dict[str, Any]) -> str:
    body = {k: v for k, v in envelope.items() if k != DIGEST_FIELD}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_budget_envelope(
    decision: BudgetDecision,
    *,
    scenario_id: str,
    trace_id: str | None = None,
    commit_sha: str | None = None,
    timestamp: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a shared evidence envelope for one budget decision."""
    verdict = "ALLOW" if decision.decision == "ALLOW" else "BLOCK"
    acs = "ALLOW" if decision.decision == "ALLOW" else "DENY"
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": decision.policy_version or "unknown",
        "policy_sha256": decision.policy_sha256 or ("sha256:" + ("0" * 64)),
        "scenario_id": scenario_id,
        "decision": decision.decision,
        "verdict": verdict,
        "acs_disposition": acs,
        "reason": decision.reason,
        "trace_id": trace_id or str(uuid4()),
        "commit_sha": resolve_commit_sha(commit_sha),
        "timestamp": timestamp or utc_now_iso(),
        "details": {
            "scope": decision.scope,
            "estimated_usd": decision.estimated_usd,
            "reserved_usd": decision.reserved_usd,
            "committed_usd": decision.committed_usd,
            "budget_usd": decision.budget_usd,
            "soft_warning": decision.soft_warning,
            "status_code": decision.status_code,
            **(details or {}),
        },
    }
    envelope[DIGEST_FIELD] = envelope_digest(envelope)
    return envelope
