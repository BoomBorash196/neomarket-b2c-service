"""Cart routes."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import List

from src.database import get_db
from src.models import CartItemModel
from src.schemas import CartItemCreate, CartItem, Cart, ProductBasic, SKUInfo
from src.services.b2b_client import b2b_client

router = APIRouter()


@router.get("", response_model=Cart)
async def get_cart(user_id: str, db: AsyncSession = Depends(get_db)):
    """Get user's shopping cart with all item details."""
    # Get cart items from DB
    result = await db.execute(
        select(CartItemModel).where(CartItemModel.user_id == user_id)
    )
    cart_items = result.scalars().all()

    if not cart_items:
        return Cart(
            user_id=user_id,
            items=[],
            total_items=0,
            total_amount=0.0
        )

    # Fetch SKU and product data from B2B
    sku_ids = [item.sku_id for item in cart_items]
    skus_data = await b2b_client.get_skus_by_ids(sku_ids)

    items = []
    total_amount = 0.0

    for cart_item in cart_items:
        sku_info = skus_data.get(cart_item.sku_id)
        if not sku_info:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"SKU {cart_item.sku_id} not found in B2B"
            )

        if not sku_info.get("is_active"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"SKU {cart_item.sku_id} is no longer available"
            )

        subtotal = sku_info["price"] * cart_item.quantity
        total_amount += subtotal

        items.append(CartItem(
            cart_item_id=cart_item.cart_item_id,
            sku_id=cart_item.sku_id,
            product_id=sku_info["product_id"],
            product_title=sku_info["product_title"],
            sku_info=SKUInfo(
                sku_id=cart_item.sku_id,
                price=sku_info["price"],
                quantity_available=sku_info["quantity_available"],
                is_active=sku_info["is_active"],
            ),
            quantity=cart_item.quantity,
            subtotal=subtotal
        ))

    return Cart(
        user_id=user_id,
        items=items,
        total_items=len(items),
        total_amount=total_amount
    )


@router.post("", response_model=Cart)
async def add_to_cart(
    item: CartItemCreate,
    user_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Add item to cart or update quantity if already exists."""
    # Check if SKU exists and is available via B2B
    sku_data = await b2b_client.get_sku_by_id(item.sku_id)
    if not sku_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SKU {item.sku_id} not found"
        )

    if not sku_data.get("is_active"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"SKU {item.sku_id} is not available"
        )

    if item.quantity > sku_data.get("quantity_available", 0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Insufficient quantity available for SKU {item.sku_id}"
        )

    # Check if item already in cart
    existing = await db.execute(
        select(CartItemModel).where(
            (CartItemModel.user_id == user_id) &
            (CartItemModel.sku_id == item.sku_id)
        )
    )
    existing_item = existing.scalar_one_or_none()

    if existing_item:
        # Update quantity
        new_quantity = existing_item.quantity + item.quantity
        if new_quantity > sku_data.get("quantity_available", 0):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Total quantity exceeds available stock"
            )
        existing_item.quantity = new_quantity
    else:
        # Add new item
        new_item = CartItemModel(
            user_id=user_id,
            sku_id=item.sku_id,
            quantity=item.quantity
        )
        db.add(new_item)

    await db.commit()
    await db.refresh(existing_item if existing_item else new_item)

    # Return updated cart
    return await get_cart(user_id=user_id, db=db)


@router.delete("/{cart_item_id}", response_model=Cart)
async def remove_from_cart(
    cart_item_id: int,
    user_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Remove item from cart."""
    result = await db.execute(
        select(CartItemModel).where(
            (CartItemModel.cart_item_id == cart_item_id) &
            (CartItemModel.user_id == user_id)
        )
    )
    cart_item = result.scalar_one_or_none()

    if not cart_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cart item not found"
        )

    await db.delete(cart_item)
    await db.commit()

    return await get_cart(user_id=user_id, db=db)


@router.put("/{cart_item_id}", response_model=Cart)
async def update_cart_item(
    cart_item_id: int,
    quantity: int,
    user_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Update quantity of cart item."""
    if quantity < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Quantity must be at least 1"
        )

    result = await db.execute(
        select(CartItemModel).where(
            (CartItemModel.cart_item_id == cart_item_id) &
            (CartItemModel.user_id == user_id)
        )
    )
    cart_item = result.scalar_one_or_none()

    if not cart_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cart item not found"
        )

    # Check availability
    sku_data = await b2b_client.get_sku_by_id(cart_item.sku_id)
    if quantity > sku_data.get("quantity_available", 0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Insufficient quantity available"
        )

    cart_item.quantity = quantity
    await db.commit()
    await db.refresh(cart_item)

    return await get_cart(user_id=user_id, db=db)


@router.delete("", response_model=Cart)
async def clear_cart(user_id: str, db: AsyncSession = Depends(get_db)):
    """Remove all items from cart."""
    await db.execute(
        CartItemModel.__table__.delete().where(CartItemModel.user_id == user_id)
    )
    await db.commit()

    return Cart(user_id=user_id, items=[], total_items=0, total_amount=0.0)
