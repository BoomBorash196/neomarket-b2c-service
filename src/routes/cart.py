"""Cart routes — B2C shopping cart with guest/auth merge, B2B enrichment, unavailable reason."""

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
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
    CartSummary,
    CheckoutItem,
    CheckoutPayload,
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

def _available_quantity(sku_info: dict) -> int:
    """Return the sellable stock from a B2B SKU payload.

    B2B defines ``active_quantity`` as ``stock_quantity - reserved_quantity``
    (see ``SKUPublicResponse``), so it is the only field that may drive cart
    availability. It must be read by **key presence**, not truthiness: a real
    ``active_quantity = 0`` means "out of stock" and must never fall back to
    the total warehouse ``stock_quantity``.
    """
    for key in ("active_quantity", "quantity_available", "stock_quantity"):
        if key not in sku_info:
            continue
        value = sku_info.get(key)
        if value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _sku_unavailable_reason(sku_info: dict) -> Optional[str]:
    """Live availability reason for a SKU (never persisted).

    Uses the canonical reason values from ``b2c/cart/openapi.yaml``:
    ``OUT_OF_STOCK``, ``PRODUCT_BLOCKED``, ``PRODUCT_DELISTED``,
    ``SKU_DISABLED`` and ``ON_MODERATION``.
    """
    status = str(sku_info.get("status") or "").upper()
    if status in {"BLOCKED", "HARD_BLOCKED"}:
        return "PRODUCT_BLOCKED"
    if status in {"DELETED", "DELISTED"}:
        return "PRODUCT_DELISTED"
    if status == "ON_MODERATION":
        return "ON_MODERATION"
    if sku_info.get("is_active") is False:
        return "SKU_DISABLED"
    return None


def _image_url(sku_info: dict) -> Optional[str]:
    images = sku_info.get("images") or []
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            return first.get("url")
    return None


def _enrich_cart_items(
    cart_items: List[CartItemModel],
    skus_data: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """Enrich every cart row with live B2B data.

    Returns ``(items, unavailable_items)``. ``items`` contains **all** cart
    rows, available and unavailable, so the response matches the cart
    specification: unavailable rows are rendered with ``available=false`` and
    an ``unavailable_reason``, but their ``line_total`` is 0 and they never
    contribute to totals. The reason is always computed live from B2B — it is
    never stored on the cart row.
    """
    items: list[dict] = []
    unavailable: list[dict] = []

    for ci in cart_items:
        cart_item_id = str(ci.cart_item_id)
        sku_id = str(ci.sku_id)
        sku_info = skus_data.get(sku_id)

        if not sku_info:
            name = f"SKU {sku_id}"
            reason = "PRODUCT_DELISTED"
            items.append({
                "cart_item_id": cart_item_id,
                "item_id": cart_item_id,
                "sku_id": sku_id,
                "product_id": "",
                "name": name,
                "product_title": name,
                "sku_name": name,
                "quantity": int(ci.quantity),
                "unit_price": 0,
                "line_total": 0,
                "available_quantity": 0,
                "available_stock": 0,
                "is_available": False,
                "available": False,
                "unavailable_reason": reason,
                "subtotal": 0.0,
            })
            unavailable.append({
                "cart_item_id": cart_item_id,
                "sku_id": sku_id,
                "quantity": int(ci.quantity),
                "product_id": "",
                "product_title": name,
                "price": 0.0,
                "unavailable_reason": reason,
            })
            continue

        price = float(sku_info.get("price") or 0)
        discount = float(sku_info.get("discount") or 0)
        unit_price = max(int(round(price - discount)), 0)
        qty_available = _available_quantity(sku_info)
        reason = _sku_unavailable_reason(sku_info)
        if reason is None and qty_available <= 0:
            reason = "OUT_OF_STOCK"

        is_available = reason is None
        line_total = unit_price * int(ci.quantity) if is_available else 0
        name = str(
            sku_info.get("name")
            or sku_info.get("product_title")
            or f"SKU {sku_id}"
        )
        product_title = str(sku_info.get("product_title") or name)
        product_id = str(sku_info.get("product_id") or "")

        items.append({
            "cart_item_id": cart_item_id,
            "item_id": cart_item_id,
            "sku_id": sku_id,
            "product_id": product_id,
            "name": name,
            "product_title": product_title,
            "sku_name": name,
            "image_url": _image_url(sku_info),
            "quantity": int(ci.quantity),
            "unit_price": unit_price,
            "line_total": line_total,
            "available_quantity": qty_available,
            "available_stock": qty_available,
            "is_available": is_available,
            "available": is_available,
            "unavailable_reason": reason,
            "subtotal": float(line_total),
        })

        if not is_available:
            unavailable.append({
                "cart_item_id": cart_item_id,
                "sku_id": sku_id,
                "quantity": int(ci.quantity),
                "product_id": product_id,
                "product_title": product_title,
                "price": price,
                "unavailable_reason": reason,
            })

    return items, unavailable


def _build_cart_response(
    user_id: str,
    items: list[dict],
    unavailable: list[dict],
) -> CartWithUnavailable:
    """Build the final cart response.

    Every cart row is present in ``items`` (as required by the cart spec),
    including unavailable ones. ``items_count`` / ``total_items`` count the
    quantities of **all** rows, while ``subtotal`` / ``total_amount`` only sum
    the available lines.
    """
    cart_items = [CartItem(**item) for item in items]
    available_items = [item for item in cart_items if item.is_available]
    total_quantity = sum(int(item.quantity) for item in cart_items)
    total_amount = float(sum(int(item.line_total) for item in available_items))

    summary = CartSummary(
        total_amount=int(total_amount),
        total_items=len(cart_items),
        total_quantity=total_quantity,
        available_items=len(available_items),
        has_unavailable_items=bool(unavailable),
        checkout_ready=bool(cart_items) and not unavailable,
        currency="RUB",
    )
    checkout_payload = CheckoutPayload(
        items=[
            CheckoutItem(
                product_id=item.product_id,
                sku_id=item.sku_id,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
            )
            for item in available_items
        ],
        total_amount=int(total_amount),
        currency="RUB",
    )

    return CartWithUnavailable(
        user_id=user_id,
        items=cart_items,
        total_items=total_quantity,
        total_amount=total_amount,
        unavailable=unavailable,
        id=user_id,
        items_count=total_quantity,
        subtotal=total_amount,
        is_valid=not unavailable,
        updated_at=datetime.now(timezone.utc),
        summary=summary,
        checkout_payload=checkout_payload,
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

    items, unavailable = _enrich_cart_items(cart_items, skus_data)
    return _build_cart_response(user_id, items, unavailable)


# =====================================================================
# POST /api/v1/cart/items
# =====================================================================

@router.post("/items", response_model=CartWithUnavailable)
async def add_to_cart(
    item: CartItemCreate,
    response: Response,
    user_id: str = Depends(get_cart_owner),
    db: AsyncSession = Depends(get_db),
):
    """Add an item to the cart.

    Repeating an SKU increments quantity (200); a new line is created (201).
    The requested quantity is checked against B2B ``active_quantity`` for both
    the first add and subsequent increments. Stock is not reserved here —
    checkout performs lazy reservation.
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

    qty_available = _available_quantity(sku_data)

    if _sku_unavailable_reason(sku_data) is not None:
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
        created = False
    else:
        new_qty = int(item.quantity)
        created = True

    # Covers both the first add (new_qty == requested quantity) and repeats.
    if new_qty > qty_available:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "QUANTITY_EXCEEDED",
                "message": f"Total quantity {new_qty} exceeds available {qty_available}",
            },
        )

    if existing_item:
        existing_item.quantity = new_qty
    else:
        db.add(
            CartItemModel(
                user_id=user_id,
                sku_id=item.sku_id,
                quantity=new_qty,
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

    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
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

    items, unavailable = _enrich_cart_items(cart_items, skus_data)
    issues: List[CartValidationIssue] = []

    for item in unavailable:
        reason = item.get("unavailable_reason")
        if reason == "OUT_OF_STOCK":
            issue_type = "OUT_OF_STOCK"
        elif reason in {"PRODUCT_BLOCKED", "SKU_DISABLED", "ON_MODERATION"}:
            issue_type = "PRODUCT_BLOCKED"
        elif reason in {"PRODUCT_DELISTED", "PRODUCT_DELETED"}:
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

    cart = _build_cart_response(user_id, items, unavailable)
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

    qty_available = _available_quantity(sku_data)
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

    Conflict resolution: MAX(guest_quantity, auth_quantity) per SKU. After the
    merge the guest rows are deleted (canon merge flow), so the guest cart
    cannot leak into a later session.
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

    if guest_user_id != auth_user_id:
        await db.execute(
            CartItemModel.__table__.delete().where(
                CartItemModel.user_id == guest_user_id
            )
        )

    await db.commit()

    return await get_cart(user_id=auth_user_id, db=db)
