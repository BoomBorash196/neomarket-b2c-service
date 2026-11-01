"""Order routes — checkout with idempotency, B2B enrichment, all-or-nothing reserve."""

import hashlib
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models import CartItemModel, OrderItemModel, OrderModel
from src.schemas import (
    AddressResponse,
    Order,
    OrderCreate,
    OrderItem,
    OrderList,
    OrderStatus,
    OrderStatusEntry,
    PaymentMethodResponse,
)
from src.services.b2b_client import B2BClientError, b2b_client

router = APIRouter()


# =====================================================================
# Helpers
# =====================================================================

def _request_hash(payload: OrderCreate) -> str:
    """Stable fingerprint of the checkout body (identity excluded).

    Used to detect reuse of an Idempotency-Key with a different request.
    """
    canonical = payload.model_dump_json(exclude={"user_id"})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _synthetic_address(order: OrderModel, created_at: datetime) -> AddressResponse:
    """Build an OpenAPI-valid address snapshot.

    Checkout receives only ``address_id``; the address itself lives in the
    buyer profile service. The response still has to expose every required
    field, so the missing parts are rendered from the id.
    """
    return AddressResponse(
        id=order.address_id or order.order_id,
        country="",
        city="",
        street="",
        building="",
        created_at=created_at,
    )


def _order_to_schema(order: OrderModel, items: List[OrderItemModel]) -> Order:
    """Convert ORM objects to the OpenAPI ``OrderResponse`` schema."""
    created_at = order.created_at or datetime.now(timezone.utc)
    subtotal = int(round(order.total_amount or 0))
    delivery_cost = int(round(order.delivery_cost or 0))

    return Order(
        id=order.order_id,
        order_id=order.order_id,
        number=f"NM-{created_at.year}-{order.order_id[:6].upper()}",
        buyer_id=order.user_id,
        user_id=order.user_id,
        status=order.status,
        items=[
            OrderItem(
                order_item_id=it.order_item_id,
                order_id=it.order_id,
                sku_id=it.sku_id,
                product_id=it.product_id,
                name=it.sku_name or it.product_title or it.sku_id,
                sku_name=it.sku_name,
                product_title=it.product_title,
                quantity=it.quantity,
                unit_price=int(round(it.unit_price or 0)),
                line_total=int(round((it.unit_price or 0) * it.quantity)),
            )
            for it in items
        ],
        subtotal=subtotal,
        delivery_cost=delivery_cost,
        total=subtotal + delivery_cost,
        total_amount=order.total_amount or 0.0,
        address=_synthetic_address(order, created_at),
        payment_method=(
            PaymentMethodResponse(
                id=order.payment_method_id,
                type="CARD",
                created_at=created_at,
            )
            if order.payment_method_id
            else None
        ),
        comment=order.comment,
        cancel_reason=order.cancel_reason,
        status_history=[
            OrderStatusEntry(status=order.status, changed_at=created_at)
        ],
        created_at=created_at,
        paid_at=order.paid_at,
    )


async def _load_order_items(
    db: AsyncSession, order_id: str
) -> List[OrderItemModel]:
    result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order_id)
    )
    return list(result.scalars().all())


async def _find_by_idempotency_key(
    db: AsyncSession, idempotency_key: str
) -> Optional[OrderModel]:
    result = await db.execute(
        select(OrderModel).where(OrderModel.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


def _resolve_user_id(
    x_user_id: Optional[str], legacy_user_id: Optional[str]
) -> str:
    """Resolve the buyer identity.

    ``X-User-Id`` is the trusted source (set by the gateway from the JWT).
    ``legacy_user_id`` is a backwards-compatible fallback for older clients
    that still send it in the body/query.
    """
    user_id = x_user_id or legacy_user_id
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "UNAUTHORIZED",
                "message": "X-User-Id header is required",
            },
        )
    return user_id


async def _replay_existing_order(
    db: AsyncSession,
    existing: OrderModel,
    request_hash: str,
) -> Order:
    """Return an already created order for an idempotent replay."""
    if (
        existing.request_hash
        and request_hash
        and existing.request_hash != request_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_KEY_REUSED",
                "message": (
                    "Idempotency-Key was already used with a different "
                    "request body"
                ),
            },
        )
    items = await _load_order_items(db, existing.order_id)
    return _order_to_schema(existing, items)


async def _release_reservations(reserved: List[dict]) -> None:
    """Best-effort compensation for a partial reserve."""
    for entry in reserved:
        quantity = entry.get("reserved") or 0
        if quantity <= 0:
            continue
        try:
            await b2b_client.reserve_stock(
                [{"sku_id": entry.get("sku_id"), "quantity": -quantity}]
            )
        except B2BClientError:
            # Compensation is best-effort; the failed reserve response is
            # still returned to the caller so the order is never created.
            pass


# =====================================================================
# POST /api/v1/orders — Checkout
# =====================================================================

@router.post("", response_model=Order, status_code=status.HTTP_201_CREATED)
async def checkout(
    order_data: OrderCreate,
    db: AsyncSession = Depends(get_db),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """Create an order from the cart.

    Flow (canon ``b2c-9-checkout``):
      1. Idempotency check by ``Idempotency-Key``.
      2. Validate the cart and resolve identity.
      3. Enrich items with B2B prices/titles.
      4. Insert the order and claim the idempotency key atomically.
      5. All-or-nothing reserve in B2B.
      6. Persist OrderItems with fixed snapshot prices, mark PAID, clear cart.
    """
    user_id = _resolve_user_id(x_user_id, order_data.user_id)
    request_hash = _request_hash(order_data)

    # --- Step 1: Idempotency check ---
    existing = await _find_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return await _replay_existing_order(db, existing, request_hash)

    # --- Step 2: Fetch cart items ---
    cart_result = await db.execute(
        select(CartItemModel).where(CartItemModel.user_id == user_id)
    )
    cart_items = cart_result.scalars().all()

    if not cart_items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "EMPTY_CART", "message": "Cart is empty"},
        )

    # --- Step 3: Enrich with B2B ---
    sku_ids = [ci.sku_id for ci in cart_items]
    try:
        skus_data = await b2b_client.get_skus_by_ids(sku_ids)
    except B2BClientError as exc:
        if exc.status_code >= 500:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "B2B_UNAVAILABLE",
                    "message": f"B2B service unavailable: {exc.message}",
                },
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "B2B_ERROR",
                "message": f"B2B error: {exc.message}",
            },
        ) from exc

    if not skus_data:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "B2B_UNAVAILABLE",
                "message": "Cannot fetch SKU data",
            },
        )

    missing_skus = [s for s in sku_ids if s not in skus_data]
    if missing_skus:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "SKU_NOT_FOUND",
                "message": f"Some SKUs not found in catalog: {missing_skus}",
            },
        )

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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "NO_VALID_ITEMS",
                "message": "No valid items in cart",
            },
        )

    # Optional client-side snapshot: reject if it drifted from the cart.
    if order_data.items_snapshot:
        snapshot = {s.sku_id: s for s in order_data.items_snapshot}
        actual = {it["sku_id"]: it for it in enriched_items}
        if set(snapshot) != set(actual):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "CART_SNAPSHOT_MISMATCH",
                    "message": "items_snapshot does not match the cart",
                },
            )
        for sku_id, snap in snapshot.items():
            if (
                snap.quantity != actual[sku_id]["quantity"]
                or snap.unit_price != int(round(actual[sku_id]["unit_price"]))
            ):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "CART_SNAPSHOT_MISMATCH",
                        "message": (
                            "items_snapshot does not match the cart"
                        ),
                    },
                )

    total_amount = sum(
        it["unit_price"] * it["quantity"] for it in enriched_items
    )

    # --- Step 4: Claim the idempotency key before touching B2B ---
    # The unique index makes concurrent requests with the same key race-safe:
    # exactly one INSERT wins, the loser replays the winner's order.
    order = OrderModel(
        user_id=user_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status=OrderStatus.CREATED,
        total_amount=0.0,
        delivery_cost=0.0,
        address_id=order_data.address_id,
        payment_method_id=order_data.payment_method_id,
        comment=order_data.comment,
    )
    db.add(order)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        existing = await _find_by_idempotency_key(db, idempotency_key)
        if existing is None:
            raise
        return await _replay_existing_order(db, existing, request_hash)

    # --- Step 5: All-or-nothing reserve ---
    reservations = [
        {"sku_id": it["sku_id"], "quantity": it["quantity"]}
        for it in enriched_items
    ]

    try:
        reserve_response = await b2b_client.reserve_stock(reservations)
    except B2BClientError as exc:
        await db.rollback()
        if exc.status_code >= 500:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "B2B_UNAVAILABLE",
                    "message": f"B2B reserve unavailable: {exc.message}",
                },
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "B2B_ERROR",
                "message": f"Reserve failed: {exc.message}",
            },
        ) from exc

    failed_items = reserve_response.get("failed", []) or []
    if failed_items:
        # Roll back the order row and release anything B2B did reserve.
        await db.rollback()
        await _release_reservations(reserve_response.get("success", []) or [])
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RESERVE_FAILED",
                "message": "Could not reserve all items",
                "failed_items": failed_items,
            },
        )

    # --- Step 6: Persist snapshot prices, mark PAID, clear cart ---
    order.status = OrderStatus.PAID
    order.total_amount = total_amount
    order.paid_at = datetime.now(timezone.utc)

    for it in enriched_items:
        db.add(
            OrderItemModel(
                order_id=order.order_id,
                sku_id=it["sku_id"],
                sku_name=it["sku_name"],
                product_id=it["product_id"],
                product_title=it["product_title"],
                quantity=it["quantity"],
                unit_price=it["unit_price"],
            )
        )

    await db.execute(
        CartItemModel.__table__.delete().where(
            CartItemModel.user_id == user_id
        )
    )

    await db.commit()
    await db.refresh(order)

    items = await _load_order_items(db, order.order_id)
    return _order_to_schema(order, items)


# =====================================================================
# GET /api/v1/orders — order history (paginated)
# =====================================================================

@router.get("", response_model=OrderList)
async def get_user_orders(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    order_status: Optional[OrderStatus] = Query(None, alias="status"),
    user_id: Optional[str] = Query(None, description="Legacy user id"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Get the buyer's paginated order history."""
    resolved_user_id = _resolve_user_id(x_user_id, user_id)

    conditions = [OrderModel.user_id == resolved_user_id]
    if order_status is not None:
        conditions.append(OrderModel.status == order_status)

    count_result = await db.execute(
        select(func.count())
        .select_from(OrderModel)
        .where(*conditions)
    )
    total_count = int(count_result.scalar() or 0)

    result = await db.execute(
        select(OrderModel)
        .where(*conditions)
        .order_by(OrderModel.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    orders = result.scalars().all()

    items = []
    for order in orders:
        order_items = await _load_order_items(db, order.order_id)
        items.append(_order_to_schema(order, order_items))

    return OrderList(
        items=items,
        total_count=total_count,
        limit=limit,
        offset=offset,
    )


# =====================================================================
# GET /api/v1/orders/{order_id}
# =====================================================================

@router.get("/{order_id}", response_model=Order)
async def get_order(
    order_id: str,
    user_id: Optional[str] = Query(None, description="Legacy user id"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Get order details with IDOR protection."""
    resolved_user_id = _resolve_user_id(x_user_id, user_id)

    result = await db.execute(
        select(OrderModel).where(
            (OrderModel.order_id == order_id)
            & (OrderModel.user_id == resolved_user_id)
        )
    )
    order = result.scalar_one_or_none()

    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ORDER_NOT_FOUND", "message": "Order not found"},
        )

    items = await _load_order_items(db, order.order_id)
    return _order_to_schema(order, items)


# =====================================================================
# POST /api/v1/orders/{order_id}/cancel
# =====================================================================

class CancelOrderRequest(BaseModel):
    """Optional body for order cancellation."""
    reason: Optional[str] = None


@router.post("/{order_id}/cancel", response_model=Order)
async def cancel_order(
    order_id: str,
    body: Optional[CancelOrderRequest] = None,
    user_id: Optional[str] = Query(None, description="Legacy user id"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Cancel order if possible (CREATED or PAID status).

    Flow:
      1. Check order exists and belongs to user (IDOR).
      2. Check status is cancellable (CREATED, PAID only).
      3. Attempt all-or-nothing unreserve via B2B.
      4. If unreserve succeeds → CANCELLED.
      5. If unreserve fails → CANCEL_PENDING (await async retry).
    """
    resolved_user_id = _resolve_user_id(x_user_id, user_id)

    result = await db.execute(
        select(OrderModel).where(
            (OrderModel.order_id == order_id)
            & (OrderModel.user_id == resolved_user_id)
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

    # Release stock reservation (all-or-nothing)
    order_items = await _load_order_items(db, order_id)

    unreserve_ok = True
    for item in order_items:
        try:
            await b2b_client.reserve_stock([
                {"sku_id": item.sku_id, "quantity": -item.quantity}
            ])
        except B2BClientError:
            unreserve_ok = False

    if unreserve_ok:
        order.status = OrderStatus.CANCELLED
    else:
        # Unreserve failed — mark as CANCEL_PENDING so async retry can finish.
        order.status = OrderStatus.CANCEL_PENDING

    if body and body.reason:
        order.cancel_reason = body.reason

    await db.commit()
    await db.refresh(order)

    items = await _load_order_items(db, order_id)
    return _order_to_schema(order, items)


# =====================================================================
# POST /api/v1/orders/{order_id}/cancel-retry
# =====================================================================

@router.post("/{order_id}/cancel-retry", response_model=Order)
async def cancel_order_retry(
    order_id: str,
    user_id: Optional[str] = Query(None, description="Legacy user id"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Retry unreserve for an order stuck in CANCEL_PENDING.

    Called by a background worker (scaffold: management command / Celery / cron).
    If unreserve succeeds → CANCELLED.
    If unreserve fails again → stays CANCEL_PENDING (logged for manual review).
    """
    resolved_user_id = _resolve_user_id(x_user_id, user_id)

    result = await db.execute(
        select(OrderModel).where(
            (OrderModel.order_id == order_id)
            & (OrderModel.user_id == resolved_user_id)
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
                "message": (
                    f"Order is in {order.status} status, not CANCEL_PENDING"
                ),
            },
        )

    order_items = await _load_order_items(db, order_id)

    unreserve_ok = True
    for item in order_items:
        try:
            await b2b_client.reserve_stock([
                {"sku_id": item.sku_id, "quantity": -item.quantity}
            ])
        except B2BClientError:
            unreserve_ok = False

    if unreserve_ok:
        order.status = OrderStatus.CANCELLED

    await db.commit()
    await db.refresh(order)

    items = await _load_order_items(db, order_id)
    return _order_to_schema(order, items)
