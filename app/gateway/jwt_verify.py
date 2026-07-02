"""Verify OIDC JWT signatures against a JWKS endpoint (Keycloak, Entra ID, etc.)."""

from __future__ import annotations

import base64
import json
import os
from functools import lru_cache
from typing import Any

import jwt
from jwt import PyJWKClient


def is_jwt_verify_enabled() -> bool:
    return os.getenv("OIDC_JWT_VERIFY", "").strip().lower() in {"1", "true", "yes"}


def get_jwks_url() -> str | None:
    url = os.getenv("OIDC_JWKS_URL", "").strip()
    return url or None


def decode_unsigned_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("JWT must have three segments")
    padding = "=" * (-len(parts[1]) % 4)
    payload = base64.urlsafe_b64decode(parts[1] + padding)
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("JWT payload must be a JSON object")
    return decoded


@lru_cache(maxsize=1)
def get_jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True)


def verify_bearer_token(token: str) -> dict[str, Any]:
    if not is_jwt_verify_enabled():
        return decode_unsigned_payload(token)

    jwks_url = get_jwks_url()
    if not jwks_url:
        return decode_unsigned_payload(token)

    client = get_jwks_client(jwks_url)
    signing_key = client.get_signing_key_from_jwt(token)
    decode_kwargs: dict[str, Any] = {
        "algorithms": ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
    }
    audience = os.getenv("OIDC_JWT_AUDIENCE", "").strip()
    issuer = os.getenv("OIDC_JWT_ISSUER", "").strip()
    if audience:
        decode_kwargs["audience"] = audience
    if issuer:
        decode_kwargs["issuer"] = issuer

    decoded = jwt.decode(token, signing_key.key, **decode_kwargs)
    if not isinstance(decoded, dict):
        raise ValueError("JWT payload must be a JSON object")
    return decoded
