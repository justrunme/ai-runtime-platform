"""Unified authentication boundary for non-public gateway routes."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.gateway.config import GatewaySettings
from app.gateway.errors import json_error_response
from app.gateway.jwt_verify import (
    JwtConfigurationError,
    is_jwt_verify_enabled,
    verify_bearer_token_async,
)

PUBLIC_PATHS = frozenset({"/healthz", "/livez", "/readyz", "/metrics"})


def request_is_authorized(request: Request, api_keys: frozenset[str]) -> bool:
    """Accept a bearer token or x-api-key header against the configured key set."""
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and header.removeprefix("Bearer ").strip() in api_keys:
        return True
    return request.headers.get("x-api-key", "") in api_keys


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


async def authenticate_request(request: Request, settings: GatewaySettings) -> JSONResponse | None:
    """Return an error response when authentication fails; otherwise None."""
    if request.url.path in PUBLIC_PATHS:
        return None

    request_id = request.headers.get("x-request-id")

    if is_jwt_verify_enabled() or settings.jwt_verify_enabled:
        token = _bearer_token(request)
        if not token:
            return json_error_response(
                401,
                code="missing_bearer_token",
                message="Bearer token required when OIDC_JWT_VERIFY is enabled",
                error_type="authentication_error",
                request_id=request_id,
            )
        try:
            claims = await verify_bearer_token_async(token)
        except JwtConfigurationError as error:
            return json_error_response(
                503,
                code="jwt_misconfigured",
                message=str(error),
                error_type="authentication_error",
                request_id=request_id,
            )
        except (TypeError, ValueError, json.JSONDecodeError, jwt.PyJWTError):
            return json_error_response(
                401,
                code="invalid_bearer_token",
                message="Bearer token failed verification",
                error_type="authentication_error",
                request_id=request_id,
            )
        request.state.identity_claims = claims
        request.state.auth_method = "jwt"
        return None

    if settings.api_keys:
        if not request_is_authorized(request, settings.api_keys):
            return json_error_response(
                401,
                code="invalid_api_key",
                message="missing or invalid API key",
                error_type="authentication_error",
                request_id=request_id,
            )
        request.state.auth_method = "api_key"
        request.state.identity_claims = {}
        return None

    if settings.require_auth:
        return json_error_response(
            401,
            code="authentication_required",
            message="authentication is required for this gateway profile",
            error_type="authentication_error",
            request_id=request_id,
        )

    request.state.auth_method = "none"
    request.state.identity_claims = {}
    return None


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Fail-closed auth for every non-public route when JWT or API keys are configured."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings: GatewaySettings | None = getattr(request.app.state, "settings", None)
        if settings is None:
            # Lifespan not ready yet — keep probes open, deny application routes.
            if request.url.path in PUBLIC_PATHS:
                return await call_next(request)
            return json_error_response(
                503,
                code="gateway_not_ready",
                message="gateway settings are not loaded",
                error_type="api_error",
            )
        error = await authenticate_request(request, settings)
        if error is not None:
            return error
        return await call_next(request)


def install_authentication(
    app: Any, *, middleware_cls: Callable = AuthenticationMiddleware
) -> None:
    app.add_middleware(middleware_cls)
