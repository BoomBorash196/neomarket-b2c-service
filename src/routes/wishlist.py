"""Wishlist routes."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import List

from src.database import get_db
from src.models import WishlistItemModel
from src.schemas import WishlistItemCreate, WishlistItem, Wishlist
from src.services.b2b_client import b2b_client

router = APIRouter()


@router.get("", response_model=Wishlist)
async def get_wishlist(user_id: str, db: AsyncSession = Depends(get_db)):
    """Get user's wishlist with product details."""
    result = await db.execute(
        select(WishlistItemModel).where(
            WishlistItemModel.user_id == user_id
        )
    )
    wishlist_items = result.scalars().all()

    if not wishlist_items:
        return Wishlist(user_id=user_id, items=[])

    # Fetch product data from B2B
    product_ids = [item.product_id for item in wishlist_items]
    items = []

    for item in wishlist_items:
        product_data = await b2b_client.get_product_by_id(item.product_id)
        if product_data:
            items.append(WishlistItem(
                wishlist_item_id=item.wishlist_item_id,
                user_id=user_id,
                product_id=item.product_id,
                product_title=product_data.get("title", "Unknown"),
                main_image_url=product_data.get("main_image_url", ""),
                min_price=product_data.get("min_price", 0.0),
                is_available=product_data.get("is_available", False),
                added_at=item.created_at
            ))

    return Wishlist(user_id=user_id, items=items)


@router.post("", response_model=Wishlist)
async def add_to_wishlist(
    item: WishlistItemCreate,
    user_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Add product to wishlist."""
    # Check if product exists
    product_data = await b2b_client.get_product_by_id(item.product_id)
    if not product_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"}
        )

    # Check if already in wishlist
    existing = await db.execute(
        select(WishlistItemModel).where(
            and_(
                WishlistItemModel.user_id == user_id,
                WishlistItemModel.product_id == item.product_id
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "ALREADY_IN_WISHLIST", "message": "Product already in wishlist"}
        )

    new_item = WishlistItemModel(
        user_id=user_id,
        product_id=item.product_id
    )
    db.add(new_item)
    await db.commit()
    await db.refresh(new_item)

    return await get_wishlist(user_id=user_id, db=db)


@router.delete("/{product_id}", response_model=Wishlist)
async def remove_from_wishlist(
    product_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Remove product from wishlist."""
    result = await db.execute(
        select(WishlistItemModel).where(
            and_(
                WishlistItemModel.user_id == user_id,
                WishlistItemModel.product_id == product_id
            )
        )
    )
    item = result.scalar_one_or_none()

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "WISHLIST_ITEM_NOT_FOUND", "message": "Wishlist item not found"}
        )

    await db.delete(item)
    await db.commit()

    return await get_wishlist(user_id=user_id, db=db)
