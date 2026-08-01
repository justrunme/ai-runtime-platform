"""Unified error envelope for the Runtime Gateway API."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse


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
        payload.update(extra)
    return payload


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
    raise HTTPException(
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
