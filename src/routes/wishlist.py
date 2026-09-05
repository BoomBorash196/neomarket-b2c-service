"""Wishlist/Favorites routes — B2C favorites with B2B enrichment.

Secure identity: user_id comes ONLY from X-User-Id header (JWT proxy).
Query/body user_id is ALWAYS ignored to prevent IDOR.
"""

from fastapi import Response, APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import List, Optional

from src.database import get_db
from src.models import WishlistItemModel
from src.schemas import (
    WishlistItemCreate,
    WishlistItem,
    Wishlist,
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
    This prevents IDOR — a user cannot view another user's wishlist
    by passing ?user_id=... in the query string.
    """
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_AUTH", "message": "X-User-Id header required"},
        )
    return x_user_id


# =====================================================================
# GET /api/v1/wishlist
# =====================================================================

@router.get("", response_model=Wishlist)
async def get_wishlist(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Get user's wishlist with B2B-enriched product details.

    Blocked/deleted products from B2B are excluded from the response.
    """
    result = await db.execute(
        select(WishlistItemModel).where(
            WishlistItemModel.user_id == user_id
        )
    )
    wishlist_items = result.scalars().all()

    if not wishlist_items:
        return Wishlist(user_id=user_id, items=[])

    items: List[WishlistItem] = []
    for item in wishlist_items:
        product_data = await b2b_client.get_product_by_id(item.product_id)
        if product_data is None:
            # Product blocked/deleted in B2B — skip it, don't expose
            continue
        items.append(WishlistItem(
            wishlist_item_id=str(item.wishlist_item_id),
            user_id=user_id,
            product_id=item.product_id,
            product_title=product_data.get("title", "Unknown"),
            main_image_url=product_data.get("main_image_url", ""),
            min_price=product_data.get("min_price", 0.0),
            is_available=product_data.get("is_available", False),
            added_at=item.created_at,
        ))

    return Wishlist(user_id=user_id, items=items)


# =====================================================================
# POST /api/v1/wishlist — idempotent
# =====================================================================

def _has_stock(product_data: dict) -> bool:
    """Compute availability from B2B SKUs (active_quantity > 0)."""
    skus = product_data.get("skus", [])
    if not skus:
        return bool(product_data.get("has_stock", False))
    return any((s.get("active_quantity", 0) or 0) > 0 for s in skus if isinstance(s, dict))


@router.post("", response_model=WishlistItem, status_code=status.HTTP_201_CREATED)
async def add_to_wishlist(
    response: Response,
    item: WishlistItemCreate,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Add product to wishlist (idempotent).

    - First add → 201 CREATED
    - Already exists → 200 OK (return existing, no duplicate)
    - Product not found in B2B → 404
    """
    # Check product exists in B2B
    product_data = await b2b_client.get_product_by_id(item.product_id)
    if not product_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )

    # Check if already in wishlist — idempotent: return existing.
    # Restart the transaction so the select sees rows committed by
    # previous requests (single-connection aiosqlite in tests, and
    # READ COMMITTED safety in production).
    await db.rollback()
    existing = await db.execute(
        select(WishlistItemModel).where(
            and_(
                WishlistItemModel.user_id == user_id,
                WishlistItemModel.product_id == item.product_id,
            )
        )
    )
    existing_item = existing.scalar_one_or_none()
    if existing_item:
        response.status_code = status.HTTP_200_OK
        return WishlistItem(
            wishlist_item_id=str(existing_item.wishlist_item_id),
            user_id=user_id,
            product_id=existing_item.product_id,
            product_title=product_data.get("title", "Unknown"),
            main_image_url=product_data.get("cover_image") or "",
            min_price=product_data.get("min_price", 0.0),
            is_available=_has_stock(product_data),
            added_at=existing_item.created_at,
        )

    # Re-check inside this transaction: a row committed by another session
    # may not have been visible to the earlier select.
    recheck = await db.execute(
        select(WishlistItemModel).where(
            and_(
                WishlistItemModel.user_id == user_id,
                WishlistItemModel.product_id == item.product_id,
            )
        )
    )
    existing_item = recheck.scalar_one_or_none()
    if existing_item:
        response.status_code = status.HTTP_200_OK
        return WishlistItem(
            wishlist_item_id=str(existing_item.wishlist_item_id),
            user_id=user_id,
            product_id=existing_item.product_id,
            product_title=product_data.get("title", "Unknown"),
            main_image_url=product_data.get("cover_image") or "",
            min_price=product_data.get("min_price", 0.0),
            is_available=_has_stock(product_data),
            added_at=existing_item.created_at,
        )

    # Insert new item; on unique-violation race, return 200 with existing row
    new_item = WishlistItemModel(
        user_id=user_id,
        product_id=item.product_id,
    )
    db.add(new_item)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        re_select = await db.execute(
            select(WishlistItemModel).where(
                and_(
                    WishlistItemModel.user_id == user_id,
                    WishlistItemModel.product_id == item.product_id,
                )
            )
        )
        existing_item = re_select.scalar_one()
        response.status_code = status.HTTP_200_OK
        return WishlistItem(
            wishlist_item_id=str(existing_item.wishlist_item_id),
            user_id=user_id,
            product_id=existing_item.product_id,
            product_title=product_data.get("title", "Unknown"),
            main_image_url=product_data.get("cover_image") or "",
            min_price=product_data.get("min_price", 0.0),
            is_available=_has_stock(product_data),
            added_at=existing_item.created_at,
        )
    await db.refresh(new_item)

    return WishlistItem(
        wishlist_item_id=str(new_item.wishlist_item_id),
        user_id=user_id,
        product_id=new_item.product_id,
        product_title=product_data.get("title", "Unknown"),
        main_image_url=product_data.get("cover_image") or "",
        min_price=product_data.get("min_price", 0.0),
        is_available=_has_stock(product_data),
        added_at=new_item.created_at,
    )


# =====================================================================
# DELETE /api/v1/wishlist/{product_id} — idempotent
# =====================================================================

@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_wishlist(
    product_id: str,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Remove product from wishlist (idempotent).

    - Exists → 204 NO CONTENT
    - Already absent → 204 NO CONTENT (idempotent)
    """
    result = await db.execute(
        select(WishlistItemModel).where(
            and_(
                WishlistItemModel.user_id == user_id,
                WishlistItemModel.product_id == product_id,
            )
        )
    )
    item = result.scalar_one_or_none()

    if item:
        await db.delete(item)
        await db.commit()

    return None  # 204
