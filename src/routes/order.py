"""Order routes — checkout with idempotency, B2B enrichment, all-or-nothing reserve."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from typing import List
import uuid

from src.database import get_db
from src.models import OrderModel, OrderItemModel, CartItemModel
from src.schemas import (
    OrderCreate,
    Order,
    OrderItem,
    OrderStatus,
)
from src.services.b2b_client import b2b_client, B2BClientError

router = APIRouter()


# =====================================================================
# Helpers
# =====================================================================

def _order_to_schema(order: OrderModel, items: List[OrderItemModel]) -> Order:
    """Convert ORM objects to response schema."""
    return Order(
        order_id=order.order_id,
        user_id=order.user_id,
        status=order.status,
        total_amount=order.total_amount,
        created_at=order.created_at,
        items=[
            OrderItem(
                order_item_id=it.order_item_id,
                order_id=it.order_id,
                sku_id=it.sku_id,
                sku_name=it.sku_name,
                product_id=it.product_id,
                product_title=it.product_title,
                quantity=it.quantity,
                unit_price=it.unit_price,
            )
            for it in items
        ],
    )


# =====================================================================
# POST /api/v1/orders — Checkout
# =====================================================================

@router.post("", response_model=Order)
async def checkout(
    body: OrderCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create order from cart with idempotency and all-or-nothing reserve.

    Flow:
      1. Check idempotency_key — if existing order found, return it.
      2. Fetch cart items for user.
      3. Enrich cart items with B2B data (prices, titles).
      4. All-or-nothing reserve via B2B.
      5. Create Order + OrderItems with fixed snapshot prices.
      6. Clear cart.
    """
    # --- Step 1: Idempotency check ---
    if body.idempotency_key:
        existing = await db.execute(
            select(OrderModel).where(OrderModel.idempotency_key == body.idempotency_key)
        )
        existing_order = existing.scalar_one_or_none()
        if existing_order:
            result = await db.execute(
                select(OrderItemModel).where(OrderItemModel.order_id == existing_order.order_id)
            )
            items = result.scalars().all()
            return _order_to_schema(existing_order, items)

    # --- Step 2: Fetch cart items ---
    cart_result = await db.execute(
        select(CartItemModel).where(CartItemModel.user_id == body.user_id)
    )
    cart_items = cart_result.scalars().all()

    if not cart_items:
        raise HTTPException(
            status_code=400,
            detail={"error": "EMPTY_CART", "message": "Cart is empty"},
        )

    # --- Step 3: Enrich with B2B ---
    sku_ids = [ci.sku_id for ci in cart_items]
    try:
        skus_data = await b2b_client.get_skus_by_ids(sku_ids)
    except B2BClientError as exc:
        if exc.status_code >= 500:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "B2B_UNAVAILABLE",
                    "message": f"B2B service unavailable: {exc.message}",
                },
            ) from exc
        raise HTTPException(
            status_code=502,
            detail={
                "error": "B2B_ERROR",
                "message": f"B2B error: {exc.message}",
            },
        ) from exc

    if not skus_data:
        raise HTTPException(
            status_code=502,
            detail={"error": "B2B_UNAVAILABLE", "message": "Cannot fetch SKU data"},
        )

    # Validate all cart SKUs exist in B2B
    missing_skus = [s for s in sku_ids if s not in skus_data]
    if missing_skus:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "SKU_NOT_FOUND",
                "message": f"Some SKUs not found in catalog: {missing_skus}",
            },
        )

    # Build enriched order items list
    enriched_items = []
    for ci in cart_items:
        sku_info = skus_data.get(ci.sku_id)
        if not sku_info:
            continue
        enriched_items.append({
            "cart_item": ci,
            "sku_id": ci.sku_id,
            "sku_name": sku_info.get("sku_id", ci.sku_id),
            "product_id": sku_info.get("product_id", ""),
            "product_title": sku_info.get("product_title", ""),
            "quantity": ci.quantity,
            "unit_price": sku_info.get("price", 0.0),
        })

    if not enriched_items:
        raise HTTPException(
            status_code=400,
            detail={"error": "NO_VALID_ITEMS", "message": "No valid items in cart"},
        )

    # --- Step 4: All-or-nothing reserve ---
    reservations = [
        {"sku_id": it["sku_id"], "quantity": it["quantity"]}
        for it in enriched_items
    ]
    reserve_response = {}
    reserve_success = False
    failed_items = []

    try:
        reserve_response = await b2b_client.reserve_stock(reservations)
        reserve_success = reserve_response.get("success", False)
        failed_items = reserve_response.get("failed_items", [])
    except B2BClientError as exc:
        if exc.status_code >= 500:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "B2B_UNAVAILABLE",
                    "message": f"B2B reserve unavailable: {exc.message}",
                },
            ) from exc
        raise HTTPException(
            status_code=502,
            detail={"error": "B2B_ERROR", "message": f"Reserve failed: {exc.message}"},
        ) from exc

    if not reserve_success or failed_items:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "RESERVE_FAILED",
                "message": "Could not reserve all items",
                "failed_items": failed_items,
            },
        )

    # --- Step 5: Create order with fixed snapshot prices ---
    total_amount = sum(it["unit_price"] * it["quantity"] for it in enriched_items)

    order = OrderModel(
        user_id=body.user_id,
        idempotency_key=body.idempotency_key or str(uuid.uuid4()),
        status=OrderStatus.CREATED,
        total_amount=total_amount,
    )
    db.add(order)
    await db.flush()  # Get order_id

    for it in enriched_items:
        order_item = OrderItemModel(
            order_id=order.order_id,
            sku_id=it["sku_id"],
            sku_name=it["sku_name"],
            product_id=it["product_id"],
            product_title=it["product_title"],
            quantity=it["quantity"],
            unit_price=it["unit_price"],
        )
        db.add(order_item)

    # --- Step 6: Clear cart ---
    await db.execute(
        CartItemModel.__table__.delete().where(CartItemModel.user_id == body.user_id)
    )

    await db.commit()

    # Refresh and return
    await db.refresh(order)
    result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order.order_id)
    )
    items = result.scalars().all()

    return _order_to_schema(order, items)


# =====================================================================
# GET /api/v1/orders
# =====================================================================

@router.get("", response_model=List[Order])
async def get_user_orders(
    user_id: str = Query(..., description="User ID"),
    db: AsyncSession = Depends(get_db),
):
    """Get user's order history."""
    result = await db.execute(
        select(OrderModel)
        .where(OrderModel.user_id == user_id)
        .order_by(OrderModel.created_at.desc())
    )
    orders = result.scalars().all()

    response = []
    for order in orders:
        items_result = await db.execute(
            select(OrderItemModel).where(OrderItemModel.order_id == order.order_id)
        )
        items = items_result.scalars().all()
        response.append(_order_to_schema(order, items))

    return response


# =====================================================================
# GET /api/v1/orders/{order_id}
# =====================================================================

@router.get("/{order_id}", response_model=Order)
async def get_order(
    order_id: int,
    user_id: str = Query(..., description="User ID for IDOR protection"),
    db: AsyncSession = Depends(get_db),
):
    """Get order details with IDOR protection."""
    result = await db.execute(
        select(OrderModel).where(
            (OrderModel.order_id == order_id) &
            (OrderModel.user_id == user_id)
        )
    )
    order = result.scalar_one_or_none()

    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "ORDER_NOT_FOUND", "message": "Order not found"},
        )

    items_result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order.order_id)
    )
    items = items_result.scalars().all()

    return _order_to_schema(order, items)


# =====================================================================
# POST /api/v1/orders/{order_id}/cancel
# =====================================================================

@router.post("/{order_id}/cancel", response_model=Order)
async def cancel_order(
    order_id: int,
    user_id: str = Query(..., description="User ID for IDOR protection"),
    db: AsyncSession = Depends(get_db),
):
    """Cancel order if possible (CREATED or PAID status)."""
    result = await db.execute(
        select(OrderModel).where(
            (OrderModel.order_id == order_id) &
            (OrderModel.user_id == user_id)
        )
    )
    order = result.scalar_one_or_none()

    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "ORDER_NOT_FOUND", "message": "Order not found"},
        )

    cancellable_statuses = [OrderStatus.CREATED, OrderStatus.PAID]
    if order.status not in cancellable_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "CANNOT_CANCEL",
                "message": f"Cannot cancel order in {order.status} status",
            },
        )

    # Release stock reservation
    items_result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order_id)
    )
    order_items = items_result.scalars().all()

    for item in order_items:
        try:
            await b2b_client.reserve_stock([
                {"sku_id": item.sku_id, "quantity": -item.quantity}
            ])
        except B2BClientError:
            pass  # Log but don't fail — manual intervention may be needed

    order.status = OrderStatus.CANCELLED
    await db.commit()
    await db.refresh(order)

    items_result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order_id)
    )
    items = items_result.scalars().all()

    return _order_to_schema(order, items)