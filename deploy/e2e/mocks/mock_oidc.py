"""Minimal OIDC/JWKS issuer for Runtime production-profile e2e."""

from __future__ import annotations

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ISSUER = "http://mock-oidc:8080"
AUDIENCE = "ai-runtime"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()
_kid = "e2e-key-1"

app = FastAPI(title="mock-oidc")


def _jwk() -> dict[str, Any]:
    public_numbers = _public_key.public_numbers()

    def b64url_uint(value: int) -> str:
        length = (value.bit_length() + 7) // 8
        return jwt.utils.base64url_encode(value.to_bytes(length, "big")).decode()

    return {
        "kty": "RSA",
        "kid": _kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64url_uint(public_numbers.n),
        "e": b64url_uint(public_numbers.e),
    }


@app.get("/readyz")
@app.get("/healthz")
async def ready() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/jwks")
@app.get("/.well-known/jwks.json")
async def jwks() -> JSONResponse:
    return JSONResponse({"keys": [_jwk()]})


@app.post("/token")
async def token(request: Request) -> dict[str, str]:
    try:
        overrides = await request.json()
    except Exception:  # noqa: BLE001
        overrides = {}
    if not isinstance(overrides, dict):
        overrides = {}
    now = int(time.time())
    payload = {
        "sub": "e2e-svc",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 3600,
        "team": "platform",
        "preferred_username": "e2e",
        "environment": "development",
        "namespace": "ai-dev",
        "tool_access": False,
        "write_permission": False,
    }
    payload.update(overrides)
    token_value = jwt.encode(
        payload,
        _private_key,
        algorithm="RS256",
        headers={"kid": _kid},
    )
    return {"access_token": token_value, "token_type": "Bearer"}
