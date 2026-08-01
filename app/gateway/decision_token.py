"""Optional cryptographic binding for Control Plane governance decisions."""

from __future__ import annotations

import asyncio
import hmac
import os
import time
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient
from prometheus_client import Counter, Histogram

from app.gateway.evidence import EnforcementEvidence, evidence_from_governance
from app.gateway.governance import governance_detail
from app.gateway.request_digest import compute_request_digest

DECISION_JWKS_LOOKUPS = Counter(
    "gateway_decision_jwks_lookups_total",
    "JWKS lookups for Control Plane decision tokens.",
    ["outcome"],
)
DECISION_JWKS_LOOKUP_DURATION = Histogram(
    "gateway_decision_jwks_lookup_seconds",
    "Wall time for decision-token JWKS key resolution.",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)


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


@lru_cache(maxsize=4)
def get_decision_jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True, lifespan=3600)


def reset_decision_jwks_cache() -> None:
    get_decision_jwks_client.cache_clear()


def _normalize_digest(value: str) -> str:
    text = value.strip()
    if text.startswith("sha256:"):
        return text.removeprefix("sha256:")
    return text


def _verify_token_sync(token: str, jwks_url: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        client = get_decision_jwks_client(jwks_url)
        key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=decision_audience(),
            options={"require": ["exp", "aud"]},
        )
        DECISION_JWKS_LOOKUPS.labels(outcome="success").inc()
        return claims
    except Exception:
        DECISION_JWKS_LOOKUPS.labels(outcome="error").inc()
        raise
    finally:
        DECISION_JWKS_LOOKUP_DURATION.observe(time.perf_counter() - started)


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


def _compare_optional(token_value: str, local_value: str, *, field: str) -> None:
    if not token_value or not local_value:
        return
    if not hmac.compare_digest(token_value, local_value):
        raise HTTPException(
            status_code=403,
            detail=governance_detail(
                code="signed_decision_mismatch",
                message=f"signed decision {field} does not match local request",
                result={},
            ),
        )


async def bind_signed_decision(
    result: dict[str, Any],
    *,
    evaluate_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate optional decision_token against evaluate JSON and local request digest."""
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
    except HTTPException:
        raise
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
        token_value = _normalize_digest(str(claims.get(field) or ""))
        result_value = _normalize_digest(str(result.get(field) or ""))
        if token_value and result_value and not hmac.compare_digest(token_value, result_value):
            raise HTTPException(
                status_code=403,
                detail=governance_detail(
                    code="signed_decision_mismatch",
                    message=f"signed decision {field} does not match evaluate response",
                    result=result,
                ),
            )

    if evaluate_payload is not None:
        local_digest = compute_request_digest(evaluate_payload)
        token_digest = _normalize_digest(str(claims.get("request_digest") or ""))
        result_digest = _normalize_digest(str(result.get("request_digest") or ""))
        # Prefer token digest; fall back to evaluate JSON when token omits it.
        expected = token_digest or result_digest
        if expected and not hmac.compare_digest(local_digest, expected):
            raise HTTPException(
                status_code=403,
                detail=governance_detail(
                    code="signed_decision_request_mismatch",
                    message="signed decision request_digest does not match local request",
                    result=result,
                ),
            )
        # Optional claim bindings when Control Plane includes them.
        _compare_optional(
            str(claims.get("tenant") or claims.get("tenant_id") or "").strip(),
            str(evaluate_payload.get("tenant_id") or evaluate_payload.get("team") or "").strip(),
            field="tenant",
        )
        _compare_optional(
            str(claims.get("subject") or "").strip(),
            str(evaluate_payload.get("subject") or "").strip(),
            field="subject",
        )
        _compare_optional(
            str(claims.get("model") or "").strip(),
            str(evaluate_payload.get("model") or "").strip(),
            field="model",
        )

    return _merge_token_claims(result, claims)


def evidence_with_token(
    result: dict[str, Any] | None,
    *,
    enforcement_outcome: str,
) -> EnforcementEvidence:
    return evidence_from_governance(result, enforcement_outcome=enforcement_outcome)
