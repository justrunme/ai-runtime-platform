"""Minimal Control Plane stub for Runtime platform e2e."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="mock-control-plane")
_approvals: dict[str, str] = {}


@app.get("/readyz")
@app.get("/healthz")
async def ready() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/governance/evaluate")
async def evaluate(request: Request) -> JSONResponse:
    payload = await request.json()
    approval_header = request.headers.get("x-ai-approval-id", "").strip()
    prompt = str(payload.get("prompt_text") or "").lower()

    if "e2e-block" in prompt:
        return JSONResponse(
            {
                "final_verdict": "block",
                "reasons": ["e2e-block"],
                "stages": {},
                "decision_id": "dec_block",
            }
        )

    if approval_header and _approvals.get(approval_header) == "approved":
        return JSONResponse(
            {
                "final_verdict": "allow",
                "reasons": ["approved"],
                "stages": {},
                "decision_id": "dec_allow",
                "approval_id": approval_header,
            }
        )

    if "e2e-approve" in prompt:
        approval_id = "apr_e2e"
        _approvals.setdefault(approval_id, "pending")
        return JSONResponse(
            {
                "final_verdict": "approval_required",
                "approval_id": approval_id,
                "decision_id": "dec_pending",
                "policy_digest": "pol_e2e",
                "request_digest": "req_e2e",
                "reasons": ["needs-human"],
                "stages": {},
            }
        )

    return JSONResponse(
        {"final_verdict": "allow", "reasons": [], "stages": {}, "decision_id": "dec"}
    )


@app.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str) -> dict[str, str]:
    _approvals[approval_id] = "approved"
    return {"status": "approved", "approval_id": approval_id}
