"""Cart routes — B2C shopping cart with guest/auth merge, B2B enrichment, unavailable reason."""

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models import CartItemModel
from src.schemas import (
    CartItemCreate,
    CartItemUpdate,
    CartItem,
    Cart,
    CartResponse,
    SKUInfo,
    UnavailableCartItem,
    CartWithUnavailable,
    CartValidationIssue,
    CartValidationResponse,
)
from src.services.b2b_client import b2b_client, B2BClientError

router = APIRouter()


async def get_current_user_id(
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
) -> str:
    """Resolve the authenticated buyer from the trusted ``X-User-Id`` header."""
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_AUTH", "message": "X-User-Id header required"},
        )
    return x_user_id


async def get_cart_owner(
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_session_id: Optional[str] = Header(None, alias="X-Session-Id"),
) -> str:
    """Resolve the current buyer: authenticated ``X-User-Id`` or guest ``X-Session-Id``."""
    owner = x_user_id or x_session_id
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_AUTH", "message": "X-User-Id or X-Session-Id header required"},
        )
    return owner


# =====================================================================
# Helpers
# =====================================================================

def _enrich_cart_items(
    cart_items: List[CartItemModel],
    skus_data: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """Enrich cart items with B2B data.

    Returns ``(available_items, unavailable_items)``. Unavailable items carry
    ``unavailable_reason`` and never contribute to totals. The reason is always
    computed live from B2B — it is never stored on the cart row.
    """
    available: list[dict] = []
    unavailable: list[dict] = []

    for ci in cart_items:
        sku_info = skus_data.get(ci.sku_id)
        if not sku_info:
            unavailable.append({
                "cart_item_id": str(ci.cart_item_id),
                "sku_id": str(ci.sku_id),
                "quantity": int(ci.quantity),
                "product_id": "",
                "product_title": f"SKU {ci.sku_id}",
                "price": 0.0,
                "unavailable_reason": "sku_not_found",
            })
            continue

        sku_id = str(ci.sku_id)
        price = float(sku_info.get("price") or 0)
        discount = float(sku_info.get("discount") or 0)
        unit_price = int(round(price - discount))
        line_total = unit_price * int(ci.quantity)

        qty_available = int(
            sku_info.get("active_quantity")
            or sku_info.get("quantity_available")
            or sku_info.get("stock_quantity")
            or 0
        )
        is_active = bool(sku_info.get("is_active", qty_available > 0))

        if not is_active or qty_available <= 0:
            unavailable.append({
                "cart_item_id": str(ci.cart_item_id),
                "sku_id": sku_id,
                "quantity": int(ci.quantity),
                "product_id": str(sku_info.get("product_id") or ""),
                "product_title": str(sku_info.get("name") or sku_info.get("product_title") or f"SKU {sku_id}"),
                "price": price,
                "unavailable_reason": "not_active" if not is_active else "out_of_stock",
            })
            continue

        available.append({
            "cart_item_id": str(ci.cart_item_id),
            "sku_id": sku_id,
            "product_id": str(sku_info.get("product_id") or ""),
            "name": str(sku_info.get("name") or sku_info.get("product_title") or f"SKU {sku_id}"),
            "quantity": int(ci.quantity),
            "unit_price": unit_price,
            "line_total": line_total,
            "available_quantity": qty_available,
            "is_available": True,
            "subtotal": float(line_total),
        })

    return available, unavailable


def _build_cart_response(
    user_id: str,
    available: list[dict],
    unavailable: list[dict],
) -> CartWithUnavailable:
    """Build the final cart response.

    ``items_count`` and ``total_items`` are the sum of quantities for available
    lines; unavailable lines are listed separately and excluded from totals.
    """
    items = [CartItem(**item) for item in available]
    total_items = sum(int(item.get("quantity") or 0) for item in available)
    total_amount = sum(float(item.get("subtotal") or 0.0) for item in available)

    return CartWithUnavailable(
        user_id=user_id,
        items=items,
        total_items=total_items,
        total_amount=total_amount,
        unavailable=unavailable,
        id=user_id,
        items_count=total_items,
        subtotal=total_amount,
        is_valid=len(unavailable) == 0,
        updated_at=datetime.now(timezone.utc),
    )


# =====================================================================
# GET /api/v1/cart
# =====================================================================

@router.get("", response_model=CartWithUnavailable)
async def get_cart(
    user_id: str = Depends(get_cart_owner),
    db: AsyncSession = Depends(get_db),
):
    """Get the buyer's cart enriched with B2B data.

    The owner is either the authenticated buyer (``X-User-Id``) or a guest
    (``X-Session-Id``). Unavailable reasons are computed live from B2B and never
    stored in the cart.
    """
    result = await db.execute(
        select(CartItemModel).where(CartItemModel.user_id == user_id)
    )
    cart_items = result.scalars().all()

    if not cart_items:
        return _build_cart_response(user_id, [], [])

    sku_ids = [item.sku_id for item in cart_items]
    try:
        skus_data = await b2b_client.get_skus_by_ids(sku_ids)
    except B2BClientError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "B2B_UNAVAILABLE", "message": "Cannot enrich cart"},
        )

    available, unavailable = _enrich_cart_items(cart_items, skus_data)
    return _build_cart_response(user_id, available, unavailable)


# =====================================================================
# POST /api/v1/cart/items
# =====================================================================

@router.post("/items", response_model=CartWithUnavailable)
async def add_to_cart(
    item: CartItemCreate,
    user_id: str = Depends(get_cart_owner),
    db: AsyncSession = Depends(get_db),
):
    """Add an item to the cart.

    Repeating an SKU increments quantity. Stock is not reserved here — checkout
    performs lazy reservation.
    """
    try:
        sku_data = await b2b_client.get_sku_by_id(item.sku_id)
    except B2BClientError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "B2B_UNAVAILABLE", "message": "Cannot validate SKU"},
        )

    if not sku_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SKU_NOT_FOUND", "message": f"SKU {item.sku_id} not found"},
        )

    qty_available = int(
        sku_data.get("active_quantity")
        or sku_data.get("quantity_available")
        or sku_data.get("stock_quantity")
        or 0
    )
    is_active = bool(sku_data.get("is_active", qty_available > 0))

    if not is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "SKU_NOT_ACTIVE", "message": f"SKU {item.sku_id} is not available"},
        )

    if qty_available <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "SKU_OUT_OF_STOCK", "message": f"SKU {item.sku_id} is out of stock"},
        )

    existing = await db.execute(
        select(CartItemModel).where(
            (CartItemModel.user_id == user_id) & (CartItemModel.sku_id == item.sku_id)
        )
    )
    existing_item = existing.scalar_one_or_none()

    if existing_item:
        new_qty = int(existing_item.quantity) + int(item.quantity)
        if new_qty > qty_available:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "QUANTITY_EXCEEDED",
                    "message": f"Total quantity {new_qty} exceeds available {qty_available}",
                },
            )
        existing_item.quantity = new_qty
    else:
        db.add(
            CartItemModel(
                user_id=user_id,
                sku_id=item.sku_id,
                quantity=int(item.quantity),
            )
        )

    await db.commit()

    if existing_item:
        await db.refresh(existing_item)
    else:
        result = await db.execute(
            select(CartItemModel).where(
                (CartItemModel.user_id == user_id) & (CartItemModel.sku_id == item.sku_id)
            )
        )
        new_item = result.scalar_one()
        await db.refresh(new_item)

    return await get_cart(user_id=user_id, db=db)


# =====================================================================
# POST /api/v1/cart/validate
# =====================================================================

@router.post("/validate", response_model=CartValidationResponse)
async def validate_cart(
    user_id: str = Depends(get_cart_owner),
    db: AsyncSession = Depends(get_db),
):
    """Validate the current cart against B2B and return issues."""
    result = await db.execute(
        select(CartItemModel).where(CartItemModel.user_id == user_id)
    )
    cart_items = result.scalars().all()

    sku_ids = [item.sku_id for item in cart_items]
    try:
        skus_data = await b2b_client.get_skus_by_ids(sku_ids)
    except B2BClientError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "B2B_UNAVAILABLE", "message": "Cannot validate cart"},
        )

    available, unavailable = _enrich_cart_items(cart_items, skus_data)
    issues: List[CartValidationIssue] = []

    for item in unavailable:
        reason = item.get("unavailable_reason")
        if reason == "out_of_stock":
            issue_type = "OUT_OF_STOCK"
        elif reason == "not_active":
            issue_type = "PRODUCT_BLOCKED"
        elif reason == "sku_not_found":
            issue_type = "PRODUCT_DELETED"
        else:
            issue_type = "OUT_OF_STOCK"
        issues.append(
            CartValidationIssue(
                sku_id=item["sku_id"],
                type=issue_type,
                message=f"SKU {item['sku_id']} is unavailable: {reason}",
            )
        )

    cart = _build_cart_response(user_id, available, unavailable)
    return CartValidationResponse(is_valid=cart.is_valid, cart=cart, issues=issues)


# =====================================================================
# PATCH /api/v1/cart/items/{sku_id}
# =====================================================================

@router.patch("/items/{sku_id}", response_model=CartWithUnavailable)
async def update_cart_item(
    sku_id: str,
    body: CartItemUpdate,
    user_id: str = Depends(get_cart_owner),
    db: AsyncSession = Depends(get_db),
):
    """Update a cart line quantity; stock is revalidated from B2B."""
    result = await db.execute(
        select(CartItemModel).where(
            (CartItemModel.sku_id == sku_id) & (CartItemModel.user_id == user_id)
        )
    )
    cart_item = result.scalar_one_or_none()

    if not cart_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ITEM_NOT_FOUND", "message": "Cart item not found"},
        )

    try:
        sku_data = await b2b_client.get_sku_by_id(sku_id)
    except B2BClientError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "B2B_UNAVAILABLE", "message": "Cannot validate stock"},
        )

    if not sku_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SKU_NOT_FOUND", "message": "SKU no longer exists"},
        )

    qty_available = int(
        sku_data.get("active_quantity")
        or sku_data.get("quantity_available")
        or sku_data.get("stock_quantity")
        or 0
    )
    if body.quantity > qty_available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "QUANTITY_EXCEEDED",
                "message": f"Quantity {body.quantity} exceeds available {qty_available}",
            },
        )

    cart_item.quantity = body.quantity
    await db.commit()
    await db.refresh(cart_item)

    return await get_cart(user_id=user_id, db=db)


# =====================================================================
# DELETE /api/v1/cart/items/{sku_id}
# =====================================================================

@router.delete("/items/{sku_id}", response_model=CartWithUnavailable)
async def remove_from_cart(
    sku_id: str,
    user_id: str = Depends(get_cart_owner),
    db: AsyncSession = Depends(get_db),
):
    """Remove an item from the cart."""
    result = await db.execute(
        select(CartItemModel).where(
            (CartItemModel.sku_id == sku_id) & (CartItemModel.user_id == user_id)
        )
    )
    cart_item = result.scalar_one_or_none()

    if not cart_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ITEM_NOT_FOUND", "message": "Cart item not found"},
        )

    await db.delete(cart_item)
    await db.commit()

    return await get_cart(user_id=user_id, db=db)


# =====================================================================
# DELETE /api/v1/cart
# =====================================================================

@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_cart(
    user_id: str = Depends(get_cart_owner),
    db: AsyncSession = Depends(get_db),
):
    """Remove all items from the cart."""
    await db.execute(CartItemModel.__table__.delete().where(CartItemModel.user_id == user_id))
    await db.commit()


# =====================================================================
# POST /api/v1/cart/merge
# =====================================================================

@router.post("/merge", response_model=CartWithUnavailable)
async def merge_guest_cart(
    auth_user_id: str = Depends(get_current_user_id),
    guest_user_id: str = Header(..., alias="X-Session-Id"),
    db: AsyncSession = Depends(get_db),
):
    """Merge a guest cart into the authenticated buyer's cart.

    Conflict resolution: MAX(guest_quantity, auth_quantity) per SKU. Guest rows
    are not deleted after merge (idempotency-safe).
    """
    guest_result = await db.execute(
        select(CartItemModel).where(CartItemModel.user_id == guest_user_id)
    )
    guest_items = guest_result.scalars().all()

    if not guest_items:
        return await get_cart(user_id=auth_user_id, db=db)

    auth_result = await db.execute(
        select(CartItemModel).where(CartItemModel.user_id == auth_user_id)
    )
    auth_items = auth_result.scalars().all()

    auth_lookup = {ci.sku_id: ci for ci in auth_items}

    for gi in guest_items:
        if gi.sku_id in auth_lookup:
            auth_lookup[gi.sku_id].quantity = max(gi.quantity, auth_lookup[gi.sku_id].quantity)
        else:
            new_item = CartItemModel(
                user_id=auth_user_id,
                sku_id=gi.sku_id,
                quantity=gi.quantity,
            )
            db.add(new_item)
            auth_lookup[gi.sku_id] = new_item

    await db.commit()

    return await get_cart(user_id=auth_user_id, db=db)
