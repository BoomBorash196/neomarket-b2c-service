"""Order cancellation domain logic.

The cancel intent is persisted as ``CANCEL_PENDING`` *before* the B2B call,
so a crash/restart between the HTTP intent and the B2B response can never
lose the cancellation. A retry worker (cron) processes pending orders with
exponential backoff.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import OrderItemModel, OrderModel
from src.schemas import OrderStatus
from src.services.b2b_client import B2BClientError, b2b_client

logger = logging.getLogger(__name__)

# 1 min, 2 min, 4 min, ... capped at 1 hour.
MAX_BACKOFF_SECONDS = 3600


def backoff_delay(retry_count: int) -> int:
    """Exponential backoff for the next unreserve attempt."""
    exponent = max(retry_count - 1, 0)
    return min(60 * (2 ** exponent), MAX_BACKOFF_SECONDS)


def _as_aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def load_order_items(
    db: AsyncSession, order_id: str
) -> List[OrderItemModel]:
    result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order_id)
    )
    return list(result.scalars().all())


async def mark_cancel_pending(
    db: AsyncSession,
    order: OrderModel,
    reason: Optional[str] = None,
) -> None:
    """Persist the cancellation intent before calling B2B."""
    order.status = OrderStatus.CANCEL_PENDING
    if reason is not None:
        order.cancel_reason = reason
    order.cancel_retry_count = 0
    order.cancel_next_retry_at = None
    order.cancel_last_error = None
    await db.commit()
    await db.refresh(order)


async def _record_failure(
    db: AsyncSession, order: OrderModel, message: str
) -> None:
    order.status = OrderStatus.CANCEL_PENDING
    order.cancel_retry_count = (order.cancel_retry_count or 0) + 1
    order.cancel_last_error = message[:500]
    delay = backoff_delay(order.cancel_retry_count)
    order.cancel_next_retry_at = datetime.now(timezone.utc) + timedelta(
        seconds=delay
    )
    await db.commit()
    await db.refresh(order)
    logger.warning(
        "unreserve failed for order %s (attempt %s), retry in %ss: %s",
        order.order_id,
        order.cancel_retry_count,
        delay,
        message,
    )


async def attempt_unreserve(db: AsyncSession, order: OrderModel) -> bool:
    """Try to release the reservation.

    Returns ``True`` when the order is now CANCELLED, ``False`` when it stays
    in CANCEL_PENDING and a retry has been scheduled.
    """
    items = await load_order_items(db, order.order_id)
    payload = [
        {"sku_id": item.sku_id, "quantity": item.quantity}
        for item in items
    ]

    if not payload:
        # Nothing was reserved; cancel locally.
        order.status = OrderStatus.CANCELLED
        order.cancel_next_retry_at = None
        order.cancel_last_error = None
        await db.commit()
        await db.refresh(order)
        return True

    try:
        response = await b2b_client.unreserve_order(order.order_id, payload)
    except B2BClientError as exc:
        await _record_failure(db, order, exc.message)
        return False

    failed = (response or {}).get("failed") or []
    status = (response or {}).get("status")
    if failed or (status is not None and status != "UNRESERVED"):
        await _record_failure(
            db,
            order,
            f"B2B unreserve status={status!r}, failed={failed!r}",
        )
        return False

    order.status = OrderStatus.CANCELLED
    order.cancel_next_retry_at = None
    order.cancel_last_error = None
    await db.commit()
    await db.refresh(order)
    return True


async def process_cancel_retries(
    db: AsyncSession,
    limit: int = 100,
    now: Optional[datetime] = None,
) -> int:
    """Process due CANCEL_PENDING orders.

    Intended to be called by a cron/management command. Returns the number of
    orders that were attempted.
    """
    current_time = now or datetime.now(timezone.utc)

    result = await db.execute(
        select(OrderModel).where(
            OrderModel.status == OrderStatus.CANCEL_PENDING
        )
    )
    pending = list(result.scalars().all())
    due = [
        order
        for order in pending
        if order.cancel_next_retry_at is None
        or _as_aware(order.cancel_next_retry_at) <= current_time
    ][:limit]

    for order in due:
        try:
            await attempt_unreserve(db, order)
        except Exception:  # noqa: BLE001 - worker must survive one bad order
            await db.rollback()
            logger.exception(
                "unexpected error while retrying cancel for order %s",
                order.order_id,
            )

    return len(due)
