"""Verify OIDC JWT signatures against a JWKS endpoint (Keycloak, Entra ID, etc.)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from functools import lru_cache
from typing import Any

import jwt
from jwt import PyJWKClient
from prometheus_client import Counter, Histogram

JWKS_LOOKUPS = Counter(
    "gateway_jwks_lookups_total",
    "JWKS signing-key lookups performed by the gateway.",
    ["outcome"],
)
JWKS_LOOKUP_DURATION = Histogram(
    "gateway_jwks_lookup_seconds",
    "Wall time for JWKS signing-key resolution (may include network refresh).",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)


class JwtConfigurationError(RuntimeError):
    """Raised when JWT verification is enabled without a usable JWKS URL."""


def is_jwt_verify_enabled() -> bool:
    return os.getenv("OIDC_JWT_VERIFY", "").strip().lower() in {"1", "true", "yes"}


def get_jwks_url() -> str | None:
    url = os.getenv("OIDC_JWKS_URL", "").strip()
    return url or None


def get_jwt_issuer() -> str | None:
    value = os.getenv("OIDC_JWT_ISSUER", "").strip()
    return value or None


def get_jwt_audience() -> str | None:
    value = os.getenv("OIDC_JWT_AUDIENCE", "").strip()
    return value or None


def require_issuer_and_audience() -> bool:
    explicit = os.getenv("OIDC_JWT_REQUIRE_ISS_AUD", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    # Production profile always requires iss/aud even if the explicit flag was omitted.
    profile = os.getenv("GATEWAY_PROFILE", "local").strip().lower()
    return explicit or profile == "production"


def decode_unsigned_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("JWT must have three segments")
    padding = "=" * (-len(parts[1]) % 4)
    payload = base64.urlsafe_b64decode(parts[1] + padding)
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise TypeError("JWT payload must be a JSON object")
    return decoded


@lru_cache(maxsize=1)
def get_jwks_client(url: str) -> PyJWKClient:
    # cache_keys keeps keys in-process; lifespan warmup refreshes the first fetch.
    return PyJWKClient(url, cache_keys=True, lifespan=3600)


def _verify_bearer_token_sync(token: str) -> dict[str, Any]:
    if not is_jwt_verify_enabled():
        return decode_unsigned_payload(token)

    jwks_url = get_jwks_url()
    if not jwks_url:
        raise JwtConfigurationError(
            "OIDC_JWT_VERIFY is enabled but OIDC_JWKS_URL is not configured"
        )

    issuer = get_jwt_issuer()
    audience = get_jwt_audience()
    if require_issuer_and_audience() and (not issuer or not audience):
        raise JwtConfigurationError(
            "OIDC_JWT_ISSUER and OIDC_JWT_AUDIENCE are required when OIDC_JWT_REQUIRE_ISS_AUD=true"
        )

    client = get_jwks_client(jwks_url)
    started = time.perf_counter()
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        JWKS_LOOKUPS.labels(outcome="success").inc()
    except Exception:
        JWKS_LOOKUPS.labels(outcome="error").inc()
        raise
    finally:
        JWKS_LOOKUP_DURATION.observe(max(0.0, time.perf_counter() - started))

    options = {
        "require": ["exp", "iat"],
        "verify_aud": bool(audience),
        "verify_iss": bool(issuer),
    }
    decode_kwargs: dict[str, Any] = {
        "algorithms": ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
        "options": options,
    }
    if audience:
        decode_kwargs["audience"] = audience
    if issuer:
        decode_kwargs["issuer"] = issuer

    decoded = jwt.decode(token, signing_key.key, **decode_kwargs)
    if not isinstance(decoded, dict):
        raise TypeError("JWT payload must be a JSON object")
    return decoded


def verify_bearer_token(token: str) -> dict[str, Any]:
    """Synchronous JWT verification (tests and threadpool workers)."""
    return _verify_bearer_token_sync(token)


async def verify_bearer_token_async(token: str) -> dict[str, Any]:
    """Verify JWT without blocking the event loop on JWKS network refresh."""
    return await asyncio.to_thread(_verify_bearer_token_sync, token)


def clear_jwks_client_cache() -> None:
    clearer = getattr(get_jwks_client, "cache_clear", None)
    if callable(clearer):
        clearer()
