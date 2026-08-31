"""Global exception handlers."""

from fastapi import Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from typing import Any


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Handle database integrity errors."""
    error_msg = str(exc.orig) if hasattr(exc, 'orig') else str(exc)

    if 'unique' in error_msg.lower() or 'duplicate' in error_msg.lower():
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "code": "DUPLICATE_ENTRY",
                "message": "Resource already exists",
            }
        )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": "DATABASE_INTEGRITY_ERROR",
            "message": "Database integrity error",
        }
    )


async def http_exception_handler(request: Request, exc: Any) -> JSONResponse:
    """Handle generic HTTP exceptions."""
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": "INTERNAL_ERROR",
            "message": "Internal server error",
        }
    )