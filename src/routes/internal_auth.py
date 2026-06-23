"""Internal service-to-service authentication."""

from fastapi import Header, HTTPException, status

from src.config import settings


async def verify_internal_service(
    x_service_key: str = Header(..., alias="X-Service-Key"),
) -> None:
    """Validate inter-service key for internal endpoints (status updates, etc.)."""
    if x_service_key != settings.B2B_SERVICE_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_SERVICE_KEY", "message": "Invalid service key"},
        )
