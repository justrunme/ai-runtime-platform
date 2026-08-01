"""Unified OpenAI-compatible error envelope for the Runtime Gateway API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


def error_body(
    *,
    code: str,
    message: str,
    error_type: str = "api_error",
    request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {
            "type": error_type,
            "code": code,
            "message": message,
        }
    }
    if request_id:
        payload["error"]["request_id"] = request_id
        payload["request_id"] = request_id
    if extra:
        for key, value in extra.items():
            if key == "error" and isinstance(value, dict):
                payload["error"] = {**payload["error"], **value}
            else:
                payload[key] = value
    return payload


def api_error(
    status_code: int,
    *,
    code: str,
    message: str,
    error_type: str = "api_error",
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=error_body(
            code=code,
            message=message,
            error_type=error_type,
            request_id=request_id,
            extra=extra,
        ),
        headers=headers,
    )


def raise_api_error(
    status_code: int,
    *,
    code: str,
    message: str,
    error_type: str = "api_error",
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    raise api_error(
        status_code,
        code=code,
        message=message,
        error_type=error_type,
        request_id=request_id,
        headers=headers,
        extra=extra,
    )


def json_error_response(
    status_code: int,
    *,
    code: str,
    message: str,
    error_type: str = "api_error",
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_body(
            code=code,
            message=message,
            error_type=error_type,
            request_id=request_id,
            extra=extra,
        ),
        headers=headers,
    )


def normalize_error_payload(
    detail: Any, *, default_code: str, default_message: str
) -> dict[str, Any]:
    """Convert FastAPI/Starlette exception detail into a top-level OpenAI-style body."""
    if isinstance(detail, dict):
        if "error" in detail and isinstance(detail["error"], dict):
            return detail
        if "detail" in detail and isinstance(detail["detail"], dict):
            nested = detail["detail"]
            if "error" in nested:
                return nested
        return error_body(
            code=default_code,
            message=str(detail.get("message") or default_message),
            extra=detail,
        )
    if isinstance(detail, str):
        return error_body(code=default_code, message=detail)
    return error_body(code=default_code, message=default_message)


def register_exception_handlers(app: FastAPI) -> None:
    """Serve OpenAI-compatible `{error: ...}` without FastAPI's `detail` wrapper."""

    @app.exception_handler(HTTPException)
    async def fastapi_http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = request.headers.get("x-request-id")
        payload = normalize_error_payload(
            exc.detail,
            default_code="http_error",
            default_message="request failed",
        )
        if request_id:
            payload.setdefault("request_id", request_id)
            if isinstance(payload.get("error"), dict):
                payload["error"].setdefault("request_id", request_id)
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = request.headers.get("x-request-id")
        payload = normalize_error_payload(
            exc.detail,
            default_code="http_error",
            default_message="request failed",
        )
        if request_id:
            payload.setdefault("request_id", request_id)
        return JSONResponse(status_code=exc.status_code, content=payload)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return json_error_response(
            422,
            code="invalid_request",
            message="request validation failed",
            error_type="invalid_request_error",
            request_id=request.headers.get("x-request-id"),
            extra={"validation_errors": exc.errors()},
        )
