"""Resolve workload identity at the execution plane before governance evaluate."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import HTTPException, Request

from app.gateway.jwt_verify import (
    JwtConfigurationError,
    decode_unsigned_payload,
    is_jwt_verify_enabled,
    verify_bearer_token,
)

KNOWN_TEAMS = frozenset({"platform", "finance", "search"})


@dataclass(frozen=True)
class WorkloadIdentity:
    subject: str
    team: str
    owner: str
    groups: tuple[str, ...]
    policy_pack: str
    environment: str
    namespace: str
    source: str
    claims: dict[str, Any]


def is_trusted_proxy_enabled() -> bool:
    return os.getenv("IDENTITY_TRUSTED_PROXY", "").strip().lower() in {"1", "true", "yes"}


def _normalize_groups(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _team_from_groups(groups: list[str], fallback: str) -> str:
    for group in groups:
        if group in KNOWN_TEAMS:
            return group
    return fallback


def _claim_bool(claims: dict[str, Any], *keys: str) -> bool | None:
    for key in keys:
        if key not in claims:
            continue
        value = claims[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes"}
    return None


def extract_bearer_claims(authorization: str) -> dict[str, Any]:
    """Decode bearer claims.

    When JWT verification is enabled this is fail-closed: missing/invalid tokens
    raise HTTP 401 instead of falling back to headers or defaults.
    """
    verify = is_jwt_verify_enabled()
    has_bearer = authorization.lower().startswith("bearer ")
    token = authorization[7:].strip() if has_bearer else ""

    if verify:
        if not token:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "type": "authentication_error",
                        "code": "missing_bearer_token",
                        "message": "Bearer token required when OIDC_JWT_VERIFY is enabled",
                    }
                },
            )
        try:
            return verify_bearer_token(token)
        except JwtConfigurationError as error:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "authentication_error",
                        "code": "jwt_misconfigured",
                        "message": str(error),
                    }
                },
            ) from error
        except (TypeError, ValueError, json.JSONDecodeError, jwt.PyJWTError) as error:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "type": "authentication_error",
                        "code": "invalid_bearer_token",
                        "message": "Bearer token failed verification",
                    }
                },
            ) from error

    if not token:
        return {}
    try:
        return decode_unsigned_payload(token)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def resolve_workload_identity(request: Request, defaults: dict[str, str]) -> WorkloadIdentity:
    # Prefer claims already verified by AuthenticationMiddleware to avoid a second JWKS hit.
    state = getattr(request, "state", None)
    cached = getattr(state, "identity_claims", None) if state is not None else None
    if isinstance(cached, dict):
        claims = cached
    else:
        claims = extract_bearer_claims(request.headers.get("authorization", ""))
        if state is not None:
            state.identity_claims = claims
    trusted_proxy = is_trusted_proxy_enabled()
    allow_headers = trusted_proxy and not is_jwt_verify_enabled()

    header_groups = (
        [
            group.strip()
            for group in request.headers.get("x-ai-groups", "").split(",")
            if group.strip()
        ]
        if allow_headers
        else []
    )
    claim_groups = _normalize_groups(claims.get("groups"))
    groups = claim_groups or header_groups

    subject = (
        str(claims.get("sub") or claims.get("email") or "").strip()
        or (request.headers.get("x-ai-subject", "").strip() if allow_headers else "")
        or defaults["owner"]
    )
    team = (
        str(claims.get("team") or claims.get("tenant") or "").strip()
        or (request.headers.get("x-ai-team", "").strip() if allow_headers else "")
        or (request.headers.get("x-ai-tenant", "").strip() if allow_headers else "")
        or _team_from_groups(groups, defaults["team"])
    )
    owner = (
        str(claims.get("preferred_username") or claims.get("name") or "").strip()
        or (request.headers.get("x-ai-owner", "").strip() if allow_headers else "")
        or defaults["owner"]
    )
    environment = (
        str(claims.get("environment") or "").strip()
        or (request.headers.get("x-ai-environment", "").strip() if allow_headers else "")
        or defaults["environment"]
    )
    namespace = (
        str(claims.get("namespace") or "").strip()
        or (request.headers.get("x-ai-namespace", "").strip() if allow_headers else "")
        or defaults["namespace"]
    )
    policy_pack = str(claims.get("policy_pack") or "").strip() or (
        request.headers.get("x-ai-policy-pack", "").strip() if allow_headers else ""
    )

    if claims:
        source = "jwt"
    elif allow_headers and any(
        request.headers.get(name)
        for name in ("x-ai-subject", "x-ai-team", "x-ai-tenant", "x-ai-groups")
    ):
        source = "headers"
    else:
        source = "default"

    return WorkloadIdentity(
        subject=subject,
        team=team,
        owner=owner,
        groups=tuple(groups),
        policy_pack=policy_pack,
        environment=environment,
        namespace=namespace,
        source=source,
        claims=dict(claims),
    )


def claim_flag(identity: WorkloadIdentity, *keys: str) -> bool:
    value = _claim_bool(identity.claims, *keys)
    return bool(value)
