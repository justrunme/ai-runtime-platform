"""Resolve workload identity at the execution plane before governance evaluate."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from fastapi import Request

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


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("JWT must have three segments")
    padding = "=" * (-len(parts[1]) % 4)
    payload = base64.urlsafe_b64decode(parts[1] + padding)
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("JWT payload must be a JSON object")
    return decoded


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


def resolve_workload_identity(request: Request, defaults: dict[str, str]) -> WorkloadIdentity:
    claims: dict[str, Any] = {}
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            try:
                claims = _decode_jwt_payload(token)
            except (ValueError, json.JSONDecodeError):
                claims = {}

    header_groups = [
        group.strip()
        for group in request.headers.get("x-ai-groups", "").split(",")
        if group.strip()
    ]
    claim_groups = _normalize_groups(claims.get("groups"))
    groups = claim_groups or header_groups

    subject = (
        str(claims.get("sub") or claims.get("email") or "").strip()
        or request.headers.get("x-ai-subject", "").strip()
        or defaults["owner"]
    )
    team = (
        str(claims.get("team") or claims.get("tenant") or "").strip()
        or request.headers.get("x-ai-team", "").strip()
        or request.headers.get("x-ai-tenant", "").strip()
        or _team_from_groups(groups, defaults["team"])
    )
    owner = (
        str(claims.get("preferred_username") or claims.get("name") or "").strip()
        or request.headers.get("x-ai-owner", "").strip()
        or defaults["owner"]
    )
    environment = (
        str(claims.get("environment") or "").strip()
        or request.headers.get("x-ai-environment", "").strip()
        or defaults["environment"]
    )
    namespace = (
        str(claims.get("namespace") or "").strip()
        or request.headers.get("x-ai-namespace", "").strip()
        or defaults["namespace"]
    )
    policy_pack = (
        str(claims.get("policy_pack") or "").strip()
        or request.headers.get("x-ai-policy-pack", "").strip()
    )

    if claims:
        source = "jwt"
    elif any(
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
    )
