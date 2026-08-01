"""Optional cryptographic binding for Control Plane governance decisions."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

from app.gateway.evidence import EnforcementEvidence, evidence_from_governance
from app.gateway.governance import governance_detail


def require_signed_decision() -> bool:
    return os.getenv("GATEWAY_REQUIRE_SIGNED_DECISION", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def decision_jwks_url() -> str | None:
    url = os.getenv("CONTROL_PLANE_DECISION_JWKS_URL", "").strip()
    if url:
        return url
    # Reuse the OIDC JWKS when Control Plane signs with the same issuer keys.
    fallback = os.getenv("OIDC_JWKS_URL", "").strip()
    return fallback or None


def decision_audience() -> str:
    return os.getenv("CONTROL_PLANE_DECISION_AUDIENCE", "ai-runtime").strip() or "ai-runtime"


def _verify_token_sync(token: str, jwks_url: str) -> dict[str, Any]:
    client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
    key = client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        key.key,
        algorithms=["RS256", "ES256"],
        audience=decision_audience(),
        options={"require": ["exp", "aud"]},
    )


async def verify_decision_token(token: str) -> dict[str, Any]:
    jwks_url = decision_jwks_url()
    if not jwks_url:
        raise ValueError("CONTROL_PLANE_DECISION_JWKS_URL (or OIDC_JWKS_URL) is required")
    return await asyncio.to_thread(_verify_token_sync, token, jwks_url)


def _merge_token_claims(
    result: dict[str, Any],
    claims: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(result)
    if claims.get("decision_id") and not merged.get("decision_id"):
        merged["decision_id"] = claims["decision_id"]
    if claims.get("request_digest") and not merged.get("request_digest"):
        merged["request_digest"] = claims["request_digest"]
    if claims.get("policy_digest") and not merged.get("policy_digest"):
        merged["policy_digest"] = claims["policy_digest"]
    return merged


async def bind_signed_decision(result: dict[str, Any]) -> dict[str, Any]:
    """Validate optional decision_token and enrich digests from its claims."""
    token = result.get("decision_token")
    if not token:
        if require_signed_decision():
            raise HTTPException(
                status_code=503,
                detail=governance_detail(
                    code="signed_decision_required",
                    message="control plane did not return a signed decision token",
                    result=result,
                ),
            )
        return result

    try:
        claims = await verify_decision_token(str(token))
    except Exception as error:  # noqa: BLE001 - map crypto/config failures to governance deny
        raise HTTPException(
            status_code=403,
            detail=governance_detail(
                code="signed_decision_invalid",
                message="signed decision token verification failed",
                result=result,
                reason=str(error),
            ),
        ) from error

    verdict = str(claims.get("verdict") or "").strip().lower()
    result_verdict = str(result.get("final_verdict") or "").strip().lower()
    if verdict and result_verdict and verdict != result_verdict:
        raise HTTPException(
            status_code=403,
            detail=governance_detail(
                code="signed_decision_mismatch",
                message="signed decision verdict does not match evaluate response",
                result=result,
                reason=f"token={verdict} evaluate={result_verdict}",
            ),
        )

    token_decision = str(claims.get("decision_id") or "").strip()
    result_decision = str(result.get("decision_id") or "").strip()
    if token_decision and result_decision and token_decision != result_decision:
        raise HTTPException(
            status_code=403,
            detail=governance_detail(
                code="signed_decision_mismatch",
                message="signed decision id does not match evaluate response",
                result=result,
            ),
        )

    for field in ("request_digest", "policy_digest"):
        token_value = str(claims.get(field) or "").strip()
        result_value = str(result.get(field) or "").strip()
        if token_value and result_value and token_value != result_value:
            raise HTTPException(
                status_code=403,
                detail=governance_detail(
                    code="signed_decision_mismatch",
                    message=f"signed decision {field} does not match evaluate response",
                    result=result,
                ),
            )

    return _merge_token_claims(result, claims)


def evidence_with_token(
    result: dict[str, Any] | None,
    *,
    enforcement_outcome: str,
) -> EnforcementEvidence:
    return evidence_from_governance(result, enforcement_outcome=enforcement_outcome)
