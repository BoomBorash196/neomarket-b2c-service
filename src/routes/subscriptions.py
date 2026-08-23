"""Product availability change subscriptions.

Secure identity: user_id comes ONLY from X-User-Id header (JWT proxy).
Query/body user_id is ALWAYS ignored to prevent IDOR.

Events (MVP):
  - in_stock: product was out of stock, now available.

ADR summary:
  notify_on stored as a compact String(32) — single event type per row.
  Alternatives considered:
    1) ArrayField / JSON array → easy to extend, but requires DB migration
       when new event types arrive and more complex filtering.
    2) Separate events table → flexible, but adds join complexity and
       migration burden for a single-event MVP.
  Chosen: String column — simplest to implement and filter for MVP.
  Adding a new event type is a simple enum + data migration.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import Optional

from src.database import get_db
from src.models import SubscriptionModel
from src.schemas import (
    SubscriptionCreate,
    Subscription,
    NotifyOn,
)
from src.services.b2b_client import b2b_client

router = APIRouter()


# =====================================================================
# Dependency — secure user identity (IDOR protection)
# =====================================================================

async def get_current_user_id(
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> str:
    """Extract user_id from X-User-Id header (JWT proxy).

    CRITICAL: user_id is NEVER read from query params or request body.
    This prevents IDOR — a user cannot view/modify another user's
    subscriptions by passing ?user_id=... in the query string.
    """
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_AUTH", "message": "X-User-Id header required"},
        )
    return x_user_id


# =====================================================================
# POST /api/v1/cart/subscribe
# =====================================================================

@router.post(
    "/subscribe",
    response_model=Subscription,
    status_code=status.HTTP_201_CREATED,
)
async def subscribe_to_product(
    body: SubscriptionCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Subscribe to product availability changes.

    - SKU must exist in B2B → 404 if not found.
    - notify_on must be valid → 400 if empty/invalid.
    - Duplicate subscription → 409 Conflict.
    - Success → 201 with created subscription.

    Notification delivery is out of scope — subscription is persisted
    and a stub enqueue would be added when the notification module
    is ready.
    """
    # Validate SKU exists in B2B
    sku_data = await b2b_client.get_sku_by_id(body.sku_id)
    if not sku_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SKU_NOT_FOUND", "message": f"SKU {body.sku_id} not found"},
        )

    # Validate notify_on (enum validation is done by Pydantic, but guard
    # against unexpected empty strings from edge cases)
    if not body.notify_on:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_NOTIFY_ON", "message": "notify_on is required and must be a valid event type"},
        )

    # Check for duplicate subscription — 409 if exists
    existing = await db.execute(
        select(SubscriptionModel).where(
            and_(
                SubscriptionModel.user_id == user_id,
                SubscriptionModel.sku_id == body.sku_id,
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DUPLICATE_SUBSCRIPTION",
                "message": f"Subscription for SKU {body.sku_id} already exists",
            },
        )

    # Create subscription
    new_sub = SubscriptionModel(
        user_id=user_id,
        sku_id=body.sku_id,
        notify_on=body.notify_on.value,
    )
    db.add(new_sub)
    await db.commit()
    await db.refresh(new_sub)

    # Stub: notification delivery would be triggered here.
    # e.g., notification_service.enqueue_subscription(new_sub)

    return Subscription(
        subscription_id=str(new_sub.subscription_id),
        user_id=new_sub.user_id,
        sku_id=new_sub.sku_id,
        notify_on=new_sub.notify_on,
        created_at=new_sub.created_at,
    )


# =====================================================================
# DELETE /api/v1/cart/subscribe/{sku_id}
# =====================================================================

@router.delete(
    "/subscribe/{sku_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unsubscribe_from_product(
    sku_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Unsubscribe from product availability changes.

    - Exists → 204 NO CONTENT (subscription removed).
    - Already absent → 204 NO CONTENT (idempotent).
    """
    result = await db.execute(
        select(SubscriptionModel).where(
            and_(
                SubscriptionModel.user_id == user_id,
                SubscriptionModel.sku_id == sku_id,
            )
        )
    )
    sub = result.scalar_one_or_none()

    if sub:
        await db.delete(sub)
        await db.commit()

    return None  # 204
