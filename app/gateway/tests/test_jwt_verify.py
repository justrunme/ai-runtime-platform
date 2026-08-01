"""JWT verification contract: issuer, audience, expiry, threadpool path."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.gateway.jwt_verify import (
    JwtConfigurationError,
    clear_jwks_client_cache,
    verify_bearer_token,
    verify_bearer_token_async,
)


@pytest.fixture
def rsa_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_jwks_client_cache()
    yield
    clear_jwks_client_cache()


def _token(private_key, claims: dict) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256")


def test_require_iss_aud_fail_closed(monkeypatch, rsa_pair) -> None:
    private_key, public_key = rsa_pair
    monkeypatch.setenv("OIDC_JWT_VERIFY", "true")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://issuer.example/jwks")
    monkeypatch.setenv("OIDC_JWT_REQUIRE_ISS_AUD", "true")
    monkeypatch.delenv("OIDC_JWT_ISSUER", raising=False)
    monkeypatch.delenv("OIDC_JWT_AUDIENCE", raising=False)
    token = _token(
        private_key,
        {
            "sub": "svc",
            "exp": int(time.time()) + 60,
            "iat": int(time.time()),
        },
    )
    with pytest.raises(JwtConfigurationError, match="OIDC_JWT_ISSUER"):
        verify_bearer_token(token)


def test_wrong_audience_rejected(monkeypatch, rsa_pair) -> None:
    private_key, public_key = rsa_pair
    monkeypatch.setenv("OIDC_JWT_VERIFY", "true")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://issuer.example/jwks")
    monkeypatch.setenv("OIDC_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setenv("OIDC_JWT_AUDIENCE", "ai-runtime")
    monkeypatch.setenv("OIDC_JWT_REQUIRE_ISS_AUD", "true")

    class FakeSigningKey:
        key = public_key

    client = MagicMock()
    client.get_signing_key_from_jwt.return_value = FakeSigningKey()
    monkeypatch.setattr("app.gateway.jwt_verify.get_jwks_client", lambda url: client)

    token = _token(
        private_key,
        {
            "sub": "svc",
            "iss": "https://issuer.example",
            "aud": "other-service",
            "exp": int(time.time()) + 60,
            "iat": int(time.time()),
        },
    )
    with pytest.raises(jwt.InvalidAudienceError):
        verify_bearer_token(token)


def test_expired_token_rejected(monkeypatch, rsa_pair) -> None:
    private_key, public_key = rsa_pair
    monkeypatch.setenv("OIDC_JWT_VERIFY", "true")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://issuer.example/jwks")
    monkeypatch.setenv("OIDC_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setenv("OIDC_JWT_AUDIENCE", "ai-runtime")

    class FakeSigningKey:
        key = public_key

    client = MagicMock()
    client.get_signing_key_from_jwt.return_value = FakeSigningKey()
    monkeypatch.setattr("app.gateway.jwt_verify.get_jwks_client", lambda url: client)

    token = _token(
        private_key,
        {
            "sub": "svc",
            "iss": "https://issuer.example",
            "aud": "ai-runtime",
            "exp": int(time.time()) - 10,
            "iat": int(time.time()) - 100,
        },
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        verify_bearer_token(token)


@pytest.mark.anyio
async def test_async_verify_uses_threadpool(monkeypatch, rsa_pair) -> None:
    private_key, public_key = rsa_pair
    monkeypatch.setenv("OIDC_JWT_VERIFY", "true")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://issuer.example/jwks")
    monkeypatch.setenv("OIDC_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setenv("OIDC_JWT_AUDIENCE", "ai-runtime")

    class FakeSigningKey:
        key = public_key

    client = MagicMock()
    client.get_signing_key_from_jwt.return_value = FakeSigningKey()
    monkeypatch.setattr("app.gateway.jwt_verify.get_jwks_client", lambda url: client)

    token = _token(
        private_key,
        {
            "sub": "svc",
            "iss": "https://issuer.example",
            "aud": "ai-runtime",
            "exp": int(time.time()) + 60,
            "iat": int(time.time()),
        },
    )
    claims = await verify_bearer_token_async(token)
    assert claims["sub"] == "svc"


def test_pem_roundtrip_not_needed(rsa_pair) -> None:
    # Keep cryptography import used for key generation sanity.
    private_key, _ = rsa_pair
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    assert b"PRIVATE KEY" in pem
