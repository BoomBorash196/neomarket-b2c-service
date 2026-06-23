"""Order fulfill service — B2C-13: final reserve write-off on delivery."""

import logging
from typing import Callable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import async_session_maker
from src.models import OrderModel, OrderItemModel
from src.schemas import OrderStatus
from src.services.b2b_client import b2b_client, B2BClientError

logger = logging.getLogger(__name__)


def build_fulfill_reservations(order_items: List[OrderItemModel], order_id: int) -> list[dict]:
    """Build B2B fulfill payload from order line items."""
    return [
        {"sku_id": item.sku_id, "quantity": item.quantity, "order_id": order_id}
        for item in order_items
    ]


async def fulfill_order_stock(
    order: OrderModel,
    order_items: List[OrderItemModel],
    db: AsyncSession,
) -> bool:
    """Call B2B fulfill for order items. Returns True on success.

    Skips B2B when fulfill_completed is already True (client-side idempotency).
    On failure the order stays DELIVERED; caller should schedule retry.
    """
    if order.fulfill_completed:
        return True

    reservations = build_fulfill_reservations(order_items, order.order_id)
    try:
        await b2b_client.fulfill_stock(order.order_id, reservations)
    except B2BClientError as exc:
        logger.error(
            "fulfill failed for order %s: %s",
            order.order_id,
            exc.message,
        )
        return False

    order.fulfill_completed = True
    await db.flush()
    return True


async def retry_fulfill_order(order_id: int) -> bool:
    """Background retry: load order and attempt fulfill again."""
    async with async_session_maker() as db:
        result = await db.execute(
            select(OrderModel).where(OrderModel.order_id == order_id)
        )
        order = result.scalar_one_or_none()
        if not order or order.status != OrderStatus.DELIVERED or order.fulfill_completed:
            return order is not None and order.fulfill_completed

        items_result = await db.execute(
            select(OrderItemModel).where(OrderItemModel.order_id == order_id)
        )
        order_items = items_result.scalars().all()

        success = await fulfill_order_stock(order, order_items, db)
        await db.commit()
        if not success:
            logger.warning(
                "fulfill retry failed for order %s; manual /fulfill-retry or cron required",
                order_id,
            )
        return success


async def on_order_delivered(
    order: OrderModel,
    order_items: List[OrderItemModel],
    db: AsyncSession,
    schedule_retry: Optional[Callable[[int], None]] = None,
) -> None:
    """Trigger fulfill when order transitions to DELIVERED (B2C-13)."""
    success = await fulfill_order_stock(order, order_items, db)
    if not success and schedule_retry is not None:
        schedule_retry(order.order_id)
