"""Optional RS256 signing for Runtime verify responses (Control Plane closed-loop)."""

from __future__ import annotations

import base64
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


def _private_key_material() -> str:
    path = os.getenv("RUNTIME_VERIFY_PRIVATE_KEY_FILE", "").strip()
    if path:
        return Path(path).read_text(encoding="utf-8")
    b64 = os.getenv("RUNTIME_VERIFY_PRIVATE_KEY_B64", "").strip()
    if b64:
        return base64.b64decode(b64).decode("utf-8")
    return os.getenv("RUNTIME_VERIFY_PRIVATE_KEY", "").strip()


def verify_signing_enabled() -> bool:
    return bool(_private_key_material())


def require_signed_verify() -> bool:
    return os.getenv("GATEWAY_REQUIRE_SIGNED_VERIFY", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def verify_issuer() -> str:
    return os.getenv("RUNTIME_VERIFY_ISSUER", "ai-runtime").strip() or "ai-runtime"


def verify_audience() -> str:
    return os.getenv("RUNTIME_VERIFY_AUDIENCE", "ai-control-plane").strip() or "ai-control-plane"


def verify_kid() -> str:
    return os.getenv("RUNTIME_VERIFY_KID", "runtime-verify-1").strip() or "runtime-verify-1"


def verify_ttl_seconds() -> int:
    raw = os.getenv("RUNTIME_VERIFY_TTL_SECONDS", "300").strip() or "300"
    try:
        value = int(raw)
    except ValueError:
        return 300
    return max(30, min(value, 3600))


def _normalize_pem(raw: str) -> bytes:
    text = raw.strip()
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    return text.encode("utf-8")


@lru_cache(maxsize=1)
def _load_private_key() -> RSAPrivateKey | None:
    raw = _private_key_material()
    if not raw:
        return None
    key = serialization.load_pem_private_key(_normalize_pem(raw), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("RUNTIME_VERIFY_PRIVATE_KEY must be an RSA private key PEM")
    return key


def reset_verify_signing_cache() -> None:
    _load_private_key.cache_clear()


def _b64url_uint(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


def public_jwks() -> dict[str, Any]:
    """Return JWKS for Control Plane to verify Runtime verification tokens."""
    key = _load_private_key()
    if key is None:
        return {"keys": []}
    numbers = key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": verify_kid(),
                "use": "sig",
                "alg": "RS256",
                "n": _b64url_uint(numbers.n),
                "e": _b64url_uint(numbers.e),
            }
        ]
    }


def sign_verification_token(payload: dict[str, Any]) -> str | None:
    """Sign a compact verification JWT, or None when signing is not configured."""
    key = _load_private_key()
    if key is None:
        return None
    now = int(time.time())
    claims = {
        "iss": verify_issuer(),
        "aud": verify_audience(),
        "iat": now,
        "exp": now + verify_ttl_seconds(),
        "typ": "runtime_verify",
        **payload,
    }
    return jwt.encode(
        claims,
        key,
        algorithm="RS256",
        headers={"kid": verify_kid(), "typ": "JWT"},
    )
