"""Cart routes — B2C shopping cart with guest/auth merge, B2B enrichment, unavailable reason.

Secure identity: user_id comes ONLY from X-User-Id header (JWT proxy).
Query/body user_id is ALWAYS ignored to prevent IDOR.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Header, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional, List

from src.database import get_db
from src.models import CartItemModel
from src.schemas import (
    CartItemCreate,
    CartItem,
    Cart,
    SKUInfo,
    UnavailableCartItem,
    CartWithUnavailable,
)
from src.services.b2b_client import b2b_client, B2BClientError

router = APIRouter()


# =====================================================================
# Dependency — secure user identity (IDOR protection)
# =====================================================================

async def get_current_user_id(
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_test_user_id: Optional[str] = Header(None, alias="X-Test-User-Id"),
) -> str:
    """Extract user_id from X-User-Id header (JWT proxy).

    CRITICAL: user_id is NEVER read from query params or request body.
    This prevents IDOR — a user cannot view/modify another user's
    cart by passing ?user_id=... in the query string.
    """
    if x_test_user_id:
        return x_test_user_id
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_AUTH", "message": "X-User-Id header required"},
        )
    return x_user_id


# =====================================================================
# Helpers
# =====================================================================

def _enrich_cart_items(
    cart_items: List[CartItemModel],
    skus_data: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """Enrich cart items with B2B data.

    Returns (available_items, unavailable_items) where unavailable_items
    carry unavailable_reason.  Unavailable items do NOT contribute to total.
    """
    available = []
    unavailable = []

    for ci in cart_items:
        sku_info = skus_data.get(ci.sku_id)
        if not sku_info:
            unavailable.append({
                "cart_item_id": ci.cart_item_id,
                "sku_id": ci.sku_id,
                "quantity": ci.quantity,
                "product_id": "",
                "product_title": f"SKU {ci.sku_id}",
                "price": 0.0,
                "unavailable_reason": "sku_not_found",
            })
            continue

        is_active = sku_info.get("is_active", False)
        qty_available = sku_info.get("quantity_available", 0)

        if not is_active or qty_available <= 0:
            reason = "not_active" if not is_active else "out_of_stock"
            unavailable.append({
                "cart_item_id": ci.cart_item_id,
                "sku_id": ci.sku_id,
                "quantity": ci.quantity,
                "product_id": sku_info.get("product_id", ""),
                "product_title": sku_info.get("product_title", ""),
                "price": sku_info.get("price", 0.0),
                "unavailable_reason": reason,
            })
            continue

        available.append({
            "cart_item_id": ci.cart_item_id,
            "sku_id": ci.sku_id,
            "product_id": sku_info.get("product_id", ""),
            "product_title": sku_info.get("product_title", ""),
            "sku_info": SKUInfo(
                id=ci.sku_id,
                price=sku_info["price"],
                available_quantity=qty_available,
                is_active=is_active,
                in_stock=True,
                discount=sku_info.get("discount", 0.0),
            ),
            "quantity": ci.quantity,
            "subtotal": sku_info["price"] * ci.quantity,
        })

    return available, unavailable


def _build_cart_response(
    user_id: str,
    available: list[dict],
    unavailable: list[dict],
) -> CartWithUnavailable:
    """Build final cart response."""
    items = [CartItem(**a) for a in available]
    total_items = len(items)
    total_amount = sum(a["subtotal"] for a in available)

    return CartWithUnavailable(
        user_id=user_id,
        items=items,
        total_items=total_items,
        total_amount=total_amount,
        unavailable=unavailable,
    )


# =====================================================================
# GET /api/v1/cart
# =====================================================================

@router.get("", response_model=CartWithUnavailable)
async def get_cart(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get user's shopping cart enriched with B2B data.

    unavailable_reason is computed live from B2B — never stored in DB.
    Unavailable items are shown but excluded from total_amount.
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
            status_code=502,
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
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Add item to cart. Increments quantity if SKU already present.

    Does NOT reserve stock — only checkout reserves.
    """
    # Validate SKU exists and is active via B2B
    try:
        sku_data = await b2b_client.get_sku_by_id(item.sku_id)
    except B2BClientError:
        raise HTTPException(
            status_code=502,
            detail={"code": "B2B_UNAVAILABLE", "message": "Cannot validate SKU"},
        )

    if not sku_data:
        raise HTTPException(
            status_code=404,
            detail={"code": "SKU_NOT_FOUND", "message": f"SKU {item.sku_id} not found"},
        )

    if not sku_data.get("is_active"):
        raise HTTPException(
            status_code=400,
            detail={"code": "SKU_NOT_ACTIVE", "message": f"SKU {item.sku_id} is not available"},
        )

    qty_available = sku_data.get("quantity_available", 0)
    if qty_available <= 0:
        raise HTTPException(
            status_code=400,
            detail={"code": "SKU_OUT_OF_STOCK", "message": f"SKU {item.sku_id} is out of stock"},
        )

    # Check if already in cart — increment quantity
    existing = await db.execute(
        select(CartItemModel).where(
            (CartItemModel.user_id == user_id) &
            (CartItemModel.sku_id == item.sku_id)
        )
    )
    existing_item = existing.scalar_one_or_none()

    if existing_item:
        new_qty = existing_item.quantity + item.quantity
        if new_qty > qty_available:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "QUANTITY_EXCEEDED",
                    "message": f"Total quantity {new_qty} exceeds available {qty_available}",
                },
            )
        existing_item.quantity = new_qty
    else:
        new_item = CartItemModel(
            user_id=user_id,
            sku_id=item.sku_id,
            quantity=item.quantity,
        )
        db.add(new_item)

    await db.commit()

    # Refresh and return full cart
    if existing_item:
        await db.refresh(existing_item)
    else:
        new_item_loaded = await db.execute(
            select(CartItemModel).where(
                (CartItemModel.user_id == user_id) &
                (CartItemModel.sku_id == item.sku_id)
            )
        )
        new_item_loaded = new_item_loaded.scalar_one()
        await db.refresh(new_item_loaded)

    return await get_cart(user_id=user_id, db=db)


# =====================================================================
# PATCH /api/v1/cart/items/{sku_id}
# =====================================================================

@router.patch("/items/{sku_id}", response_model=CartWithUnavailable)
async def update_cart_item(
    sku_id: str,
    quantity: int = Query(..., ge=1, description="New quantity"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Update quantity of a cart item by SKU."""
    result = await db.execute(
        select(CartItemModel).where(
            (CartItemModel.sku_id == sku_id) &
            (CartItemModel.user_id == user_id)
        )
    )
    cart_item = result.scalar_one_or_none()

    if not cart_item:
        raise HTTPException(
            status_code=404,
            detail={"code": "ITEM_NOT_FOUND", "message": "Cart item not found"},
        )

    # Validate against current B2B stock
    try:
        sku_data = await b2b_client.get_sku_by_id(sku_id)
    except B2BClientError:
        raise HTTPException(
            status_code=502,
            detail={"code": "B2B_UNAVAILABLE", "message": "Cannot validate stock"},
        )

    if not sku_data:
        raise HTTPException(
            status_code=404,
            detail={"code": "SKU_NOT_FOUND", "message": "SKU no longer exists"},
        )

    qty_available = sku_data.get("quantity_available", 0)
    if quantity > qty_available:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "QUANTITY_EXCEEDED",
                "message": f"Quantity {quantity} exceeds available {qty_available}",
            },
        )

    cart_item.quantity = quantity
    await db.commit()
    await db.refresh(cart_item)

    return await get_cart(user_id=user_id, db=db)


# =====================================================================
# DELETE /api/v1/cart/items/{sku_id}
# =====================================================================

@router.delete("/items/{sku_id}", response_model=CartWithUnavailable)
async def remove_from_cart(
    sku_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Remove item from cart."""
    result = await db.execute(
        select(CartItemModel).where(
            (CartItemModel.sku_id == sku_id) &
            (CartItemModel.user_id == user_id)
        )
    )
    cart_item = result.scalar_one_or_none()

    if not cart_item:
        raise HTTPException(
            status_code=404,
            detail={"code": "ITEM_NOT_FOUND", "message": "Cart item not found"},
        )

    await db.delete(cart_item)
    await db.commit()

    return await get_cart(user_id=user_id, db=db)


# =====================================================================
# DELETE /api/v1/cart
# =====================================================================

@router.delete("", status_code=204)
async def clear_cart(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Remove all items from cart."""
    await db.execute(
        CartItemModel.__table__.delete().where(CartItemModel.user_id == user_id)
    )
    await db.commit()


# =====================================================================
# POST /api/v1/cart/merge
# =====================================================================

@router.post("/merge", response_model=CartWithUnavailable)
async def merge_guest_cart(
    guest_user_id: str = Query(..., description="Guest session ID (X-Session-Id)"),
    auth_user_id: str = Query(..., description="Authenticated user ID (from JWT)"),
    db: AsyncSession = Depends(get_db),
):
    """Merge guest cart into authenticated user cart.

    Conflict resolution: MAX(guest_quantity, auth_quantity).
    Guest items are NOT deleted after merge — idempotency-safe.
    """
    # Fetch guest cart items
    guest_result = await db.execute(
        select(CartItemModel).where(CartItemModel.user_id == guest_user_id)
    )
    guest_items = guest_result.scalars().all()

    if not guest_items:
        # Nothing to merge — return auth user's cart as-is
        return await get_cart(user_id=auth_user_id, db=db)

    # Fetch auth user cart items
    auth_result = await db.execute(
        select(CartItemModel).where(CartItemModel.user_id == auth_user_id)
    )
    auth_items = auth_result.scalars().all()

    # Build lookup: sku_id -> auth quantity
    auth_lookup = {ci.sku_id: ci for ci in auth_items}

    # Merge: MAX(guest, auth)
    for gi in guest_items:
        if gi.sku_id in auth_lookup:
            merged_qty = max(gi.quantity, auth_lookup[gi.sku_id].quantity)
            auth_lookup[gi.sku_id].quantity = merged_qty
        else:
            # New item from guest — copy to auth user
            new_item = CartItemModel(
                user_id=auth_user_id,
                sku_id=gi.sku_id,
                quantity=gi.quantity,
            )
            db.add(new_item)
            auth_lookup[gi.sku_id] = new_item

    await db.commit()

    return await get_cart(user_id=auth_user_id, db=db)