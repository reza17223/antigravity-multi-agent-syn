"""Shared coordination exceptions and protocol error wrapper."""

from __future__ import annotations

from typing import Any, Callable

from agsync.protocol import ProtocolError


class SyncError(Exception):
    """Base error mapped to HTTP status codes and structured JSON errors."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.error_code,
            "message": self.message,
        }
        if self.details:
            payload.update(self.details)
        return payload


class ConflictError(SyncError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(error_code, message, status_code=409, details=details)


class NotFoundError(SyncError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(error_code, message, status_code=404, details=details)


class ValidationError(SyncError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(error_code, message, status_code=400, details=details)


def wrap_protocol(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ProtocolError as exc:
        raise ValidationError("validation_error", str(exc)) from exc
