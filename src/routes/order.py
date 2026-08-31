"""Order routes — checkout with idempotency, B2B enrichment, all-or-nothing reserve."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from typing import List, Optional
import uuid

from src.database import get_db
from src.models import OrderModel, OrderItemModel, CartItemModel
from src.schemas import (
    OrderCreate,
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusUpdate,
)
from src.services.b2b_client import b2b_client, B2BClientError
from src.services.order_fulfill import on_order_delivered, fulfill_order_stock, retry_fulfill_order
from src.routes.internal_auth import verify_internal_service

router = APIRouter()


# =====================================================================
# Helpers
# =====================================================================

async def get_current_user_id(
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_test_user_id: Optional[str] = Header(None, alias="X-Test-User-Id"),
) -> str:
    """Extract user_id from JWT header or test header.

    Production: X-User-Id (set by API gateway / JWT middleware).
    Tests: X-Test-User-Id (simulates authenticated user).
    If neither is provided → 401.
    """
    if x_test_user_id:
        return x_test_user_id
    if x_user_id:
        return x_user_id
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "MISSING_AUTH", "message": "Authentication required"},
    )


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
    order_data: OrderCreate,
    db: AsyncSession = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_test_user_id: Optional[str] = Header(None, alias="X-Test-User-Id"),
):
    """Create order from cart with idempotency and all-or-nothing reserve.

    Flow:
      1. Check idempotency_key (from Idempotency-Key header) — if existing order found, return it.
      2. Fetch cart items for user.
      3. Enrich cart items with B2B data (prices, titles).
      4. All-or-nothing reserve via B2B.
      5. Create Order + OrderItems with fixed snapshot prices and PAID status.
      6. Clear cart.
    """
    # User identity from headers only — NEVER from request body (IDOR prevention)
    # Production: X-User-Id (set by API gateway / JWT middleware).
    # Tests: X-Test-User-Id (simulates authenticated user).
    user_id = x_test_user_id or x_user_id
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_AUTH", "message": "User ID required"},
        )

    # Generate idempotency key if not provided (client-side idempotency)
    effective_idempotency_key = idempotency_key or str(uuid.uuid4())

    # --- Step 1: Idempotency check ---
    if idempotency_key:
        existing = await db.execute(
            select(OrderModel).where(OrderModel.idempotency_key == idempotency_key)
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
        select(CartItemModel).where(CartItemModel.user_id == user_id)
    )
    cart_items = cart_result.scalars().all()

    if not cart_items:
        raise HTTPException(
            status_code=400,
            detail={"code": "EMPTY_CART", "message": "Cart is empty"},
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
                    "code": "B2B_UNAVAILABLE",
                    "message": f"B2B service unavailable: {exc.message}",
                },
            ) from exc
        raise HTTPException(
            status_code=502,
            detail={
                "code": "B2B_ERROR",
                "message": f"B2B error: {exc.message}",
            },
        ) from exc

    if not skus_data:
        raise HTTPException(
            status_code=502,
            detail={"code": "B2B_UNAVAILABLE", "message": "Cannot fetch SKU data"},
        )

    # Validate all cart SKUs exist in B2B
    missing_skus = [s for s in sku_ids if s not in skus_data]
    if missing_skus:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SKU_NOT_FOUND",
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
            "sku_name": sku_info.get("name", ci.sku_id),
            "product_id": sku_info.get("product_id", ""),
            "product_title": sku_info.get("name", ""),
            "quantity": ci.quantity,
            "unit_price": sku_info.get("price", 0.0),
        })

    if not enriched_items:
        raise HTTPException(
            status_code=400,
            detail={"code": "NO_VALID_ITEMS", "message": "No valid items in cart"},
        )

    # --- Step 4: All-or-nothing reserve ---
    reservations = [
        {"sku_id": it["sku_id"], "quantity": it["quantity"]}
        for it in enriched_items
    ]
    reserve_response = {}
    reserve_success = True
    failed_items = []

    try:
        reserve_response = await b2b_client.reserve_stock(reservations)
        # B2B returns {"success": [...], "failed": [...]}
        failed_items = reserve_response.get("failed", [])
        if failed_items:
            reserve_success = False
    except B2BClientError as exc:
        if exc.status_code >= 500:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "B2B_UNAVAILABLE",
                    "message": f"B2B reserve unavailable: {exc.message}",
                },
            ) from exc
        raise HTTPException(
            status_code=502,
            detail={"code": "B2B_ERROR", "message": f"Reserve failed: {exc.message}"},
        ) from exc

    if not reserve_success or failed_items:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RESERVE_FAILED",
                "message": "Could not reserve all items",
                "failed_items": failed_items,
            },
        )

    # --- Step 5: Create order with fixed snapshot prices — status PAID ---
    total_amount = sum(it["unit_price"] * it["quantity"] for it in enriched_items)

    order = OrderModel(
        user_id=user_id,
        idempotency_key=effective_idempotency_key,
        status=OrderStatus.PAID,
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
        CartItemModel.__table__.delete().where(CartItemModel.user_id == user_id)
    )

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # Race condition: duplicate idempotency_key from concurrent requests
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DUPLICATE_IDEMPOTENCY_KEY",
                "message": "Order with this idempotency key already exists",
            },
        ) from exc

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

@router.get("", response_model=dict)
async def get_user_orders(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(10, ge=1, le=100, description="Page size"),
    status: Optional[str] = Query(None, description="Filter by status"),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Get user's order history with pagination and IDOR protection."""
    base_query = select(OrderModel).where(OrderModel.user_id == user_id)
    if status:
        base_query = base_query.where(OrderModel.status == OrderStatus(status))

    # Total count
    count_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar()

    # Paginated results
    offset = (page - 1) * page_size
    result = await db.execute(
        base_query
        .order_by(OrderModel.created_at.desc())
        .limit(page_size)
        .offset(offset)
    )
    orders = result.scalars().all()

    response = []
    for order in orders:
        items_result = await db.execute(
            select(OrderItemModel).where(OrderItemModel.order_id == order.order_id)
        )
        items = items_result.scalars().all()
        response.append(_order_to_schema(order, items))

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "orders": response,
    }


# =====================================================================
# GET /api/v1/orders/{order_id}
# =====================================================================

@router.get("/{order_id}", response_model=Order)
async def get_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
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
            detail={"code": "ORDER_NOT_FOUND", "message": "Order not found"},
        )

    items_result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order.order_id)
    )
    items = items_result.scalars().all()

    return _order_to_schema(order, items)


# =====================================================================
# PATCH /api/v1/orders/{order_id}/status — internal status update (B2C-13)
# =====================================================================

_DELIVERED_FROM = {OrderStatus.ASSEMBLING, OrderStatus.DELIVERING}


@router.patch("/{order_id}/status", response_model=Order)
async def update_order_status(
    order_id: str,
    body: OrderStatusUpdate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_service),
):
    """Update order status (logistics). Transition to DELIVERED triggers B2B fulfill."""
    result = await db.execute(
        select(OrderModel).where(OrderModel.order_id == order_id)
    )
    order = result.scalar_one_or_none()

    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ORDER_NOT_FOUND", "message": "Order not found"},
        )

    new_status = body.status
    if new_status == order.status:
        items_result = await db.execute(
            select(OrderItemModel).where(OrderItemModel.order_id == order_id)
        )
        items = items_result.scalars().all()
        return _order_to_schema(order, items)

    if new_status == OrderStatus.DELIVERED and order.status not in _DELIVERED_FROM:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "INVALID_STATUS_TRANSITION",
                "message": f"Cannot transition from {order.status} to DELIVERED",
                "current_status": order.status.value,
            },
        )

    previous_status = order.status
    order.status = new_status

    items_result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order_id)
    )
    order_items = items_result.scalars().all()

    if new_status == OrderStatus.DELIVERED and previous_status != OrderStatus.DELIVERED:
        await on_order_delivered(
            order,
            order_items,
            db,
            schedule_retry=lambda oid: background_tasks.add_task(retry_fulfill_order, oid),
        )

    await db.commit()
    await db.refresh(order)

    return _order_to_schema(order, order_items)


# =====================================================================
# POST /api/v1/orders/{order_id}/fulfill-retry — retry failed fulfill
# =====================================================================

@router.post("/{order_id}/fulfill-retry", response_model=Order)
async def fulfill_order_retry(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_service),
):
    """Retry B2B fulfill for a DELIVERED order where fulfill did not complete."""
    result = await db.execute(
        select(OrderModel).where(OrderModel.order_id == order_id)
    )
    order = result.scalar_one_or_none()

    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ORDER_NOT_FOUND", "message": "Order not found"},
        )

    if order.status != OrderStatus.DELIVERED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "NOT_DELIVERED",
                "message": f"Order is in {order.status} status, not DELIVERED",
            },
        )

    items_result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order_id)
    )
    order_items = items_result.scalars().all()

    await fulfill_order_stock(order, order_items, db)
    await db.commit()
    await db.refresh(order)

    return _order_to_schema(order, order_items)


# =====================================================================
# POST /api/v1/orders/{order_id}/cancel
# =====================================================================

@router.post("/{order_id}/cancel", response_model=Order)
async def cancel_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
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
            detail={"code": "ORDER_NOT_FOUND", "message": "Order not found"},
        )

    cancellable_statuses = [OrderStatus.CREATED, OrderStatus.PAID]
    if order.status not in cancellable_statuses:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CANCEL_NOT_ALLOWED",
                "message": f"Cannot cancel order in {order.status} status",
                "current_status": order.status.value,
            },
        )

    # All-or-nothing unreserve
    items_result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order_id)
    )
    order_items = items_result.scalars().all()

    reservations = [
        {"sku_id": item.sku_id, "quantity": -item.quantity}
        for item in order_items
    ]

    unreserve_ok = True
    try:
        await b2b_client.reserve_stock(reservations)
    except B2BClientError:
        unreserve_ok = False

    if unreserve_ok:
        order.status = OrderStatus.CANCELLED
    else:
        order.status = OrderStatus.CANCEL_PENDING

    await db.commit()
    await db.refresh(order)

    items_result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order_id)
    )
    items = items_result.scalars().all()

    return _order_to_schema(order, items)


# =====================================================================
# POST /api/v1/orders/{order_id}/cancel — CANCEL_PENDING path
# =====================================================================

@router.post("/{order_id}/cancel-retry", response_model=Order)
async def cancel_order_retry(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Retry unreserve for an order stuck in CANCEL_PENDING."""
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
            detail={"code": "ORDER_NOT_FOUND", "message": "Order not found"},
        )

    if order.status != OrderStatus.CANCEL_PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "NOT_CANCEL_PENDING",
                "message": f"Order is in {order.status} status, not CANCEL_PENDING",
            },
        )

    items_result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order_id)
    )
    order_items = items_result.scalars().all()

    reservations = [
        {"sku_id": item.sku_id, "quantity": -item.quantity}
        for item in order_items
    ]

    unreserve_ok = True
    try:
        await b2b_client.reserve_stock(reservations)
    except B2BClientError:
        unreserve_ok = False

    if unreserve_ok:
        order.status = OrderStatus.CANCELLED

    await db.commit()
    await db.refresh(order)

    items_result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order_id)
    )
    items = items_result.scalars().all()

    return _order_to_schema(order, items)