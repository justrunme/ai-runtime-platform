"""Control Plane stub with request-bound, one-time approvals for Runtime e2e."""

from __future__ import annotations

import hashlib
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="mock-control-plane")
# approval_id -> {status, request_digest, policy_digest}
_approvals: dict[str, dict[str, str]] = {}


def _request_digest(payload: dict) -> str:
    # Mirror Control Plane intent: bind on semantic request fields, not live counters.
    material = {
        "team": payload.get("team"),
        "owner": payload.get("owner"),
        "environment": payload.get("environment"),
        "namespace": payload.get("namespace"),
        "action": payload.get("action"),
        "model": payload.get("model"),
        "provider": payload.get("provider"),
        "prompt_text": payload.get("prompt_text"),
        "cost_per_hour_usd": payload.get("cost_per_hour_usd"),
        "sensitive_data": payload.get("sensitive_data"),
        "tool_access": payload.get("tool_access"),
        "write_permission": payload.get("write_permission"),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@app.get("/readyz")
@app.get("/healthz")
async def ready() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/governance/evaluate")
async def evaluate(request: Request) -> JSONResponse:
    payload = await request.json()
    approval_header = request.headers.get("x-ai-approval-id", "").strip()
    prompt = str(payload.get("prompt_text") or "").lower()
    digest = _request_digest(payload)
    policy_digest = "pol_e2e"

    if "e2e-block" in prompt:
        return JSONResponse(
            {
                "final_verdict": "block",
                "reasons": ["e2e-block"],
                "stages": {},
                "decision_id": "dec_block",
                "policy_digest": policy_digest,
                "request_digest": digest,
            }
        )

    if approval_header:
        record = _approvals.get(approval_header)
        if record is None:
            return JSONResponse(
                {
                    "final_verdict": "approval_required",
                    "reasons": ["unknown approval id"],
                    "stages": {},
                    "decision_id": "dec_unknown_apr",
                    "policy_digest": policy_digest,
                    "request_digest": digest,
                }
            )
        if record["status"] == "consumed":
            return JSONResponse(
                {
                    "final_verdict": "approval_required",
                    "approval_id": approval_header,
                    "reasons": ["approval already consumed"],
                    "stages": {},
                    "decision_id": "dec_consumed",
                    "policy_digest": policy_digest,
                    "request_digest": digest,
                }
            )
        if record["status"] == "approved" and record["request_digest"] == digest:
            record["status"] = "consumed"
            return JSONResponse(
                {
                    "final_verdict": "allow",
                    "reasons": ["durable approval grants allow"],
                    "stages": {},
                    "decision_id": "dec_allow",
                    "approval_id": approval_header,
                    "policy_digest": policy_digest,
                    "request_digest": digest,
                }
            )
        return JSONResponse(
            {
                "final_verdict": "approval_required",
                "approval_id": approval_header,
                "reasons": ["approval request digest mismatch"],
                "stages": {},
                "decision_id": "dec_mismatch",
                "policy_digest": policy_digest,
                "request_digest": digest,
            }
        )

    if "e2e-approve" in prompt:
        approval_id = "apr_e2e"
        _approvals[approval_id] = {
            "status": "pending",
            "request_digest": digest,
            "policy_digest": policy_digest,
        }
        return JSONResponse(
            {
                "final_verdict": "approval_required",
                "approval_id": approval_id,
                "decision_id": "dec_pending",
                "policy_digest": policy_digest,
                "request_digest": digest,
                "reasons": ["needs-human"],
                "stages": {},
            }
        )

    return JSONResponse(
        {
            "final_verdict": "allow",
            "reasons": [],
            "stages": {},
            "decision_id": "dec",
            "policy_digest": policy_digest,
            "request_digest": digest,
        }
    )


@app.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str) -> dict[str, str]:
    record = _approvals.setdefault(
        approval_id,
        {"status": "pending", "request_digest": "", "policy_digest": "pol_e2e"},
    )
    record["status"] = "approved"
    return {"status": "approved", "approval_id": approval_id}
