"""Order routes."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.future import select as select_future
from typing import List
import json

from src.database import get_db
from src.models import OrderModel, OrderItemModel, CartItemModel
from src.schemas import OrderCreate, Order, OrderStatus, Cart
from src.services.b2b_client import b2b_client

router = APIRouter()


@router.post("", response_model=Order)
async def create_order(order_data: OrderCreate, db: AsyncSession = Depends(get_db)):
    """Create order from cart items."""
    # Reserve stock in B2B
    reservations = [
        {"sku_id": item.sku_id, "quantity": item.quantity}
        for item in order_data.items
    ]
    
    try:
        reserve_response = await b2b_client.reserve_stock(reservations)
        if not reserve_response.get("success"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to reserve stock. Some items may be unavailable."
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stock reservation failed: {str(e)}"
        )

    # Create order
    order = OrderModel(
        user_id=order_data.user_id,
        status=OrderStatus.CREATED,
        total_amount=order_data.total_amount
    )
    db.add(order)
    await db.flush()  # Get order_id

    # Create order items
    for item in order_data.items:
        order_item = OrderItemModel(
            order_id=order.order_id,
            sku_id=item.sku_id,
            product_id=item.sku_id.split("_")[0] if "_" in item.sku_id else item.sku_id,
            product_title=f"Product {item.sku_id}",  # Should come from B2B
            quantity=item.quantity,
            price=item.price_at_order
        )
        db.add(order_item)

    # Clear cart after successful order
    await db.execute(
        CartItemModel.__table__.delete().where(
            CartItemModel.user_id == order_data.user_id
        )
    )

    await db.commit()
    await db.refresh(order)

    # Get order items
    result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order.order_id)
    )
    order_items = result.scalars().all()

    return Order(
        order_id=order.order_id,
        user_id=order.user_id,
        status=order.status,
        total_amount=order.total_amount,
        created_at=order.created_at,
        items=[]  # Will be populated in a real implementation
    )


@router.get("", response_model=List[Order])
async def get_user_orders(
    user_id: str,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db)
):
    """Get user's order history."""
    offset = (page - 1) * page_size
    
    result = await db.execute(
        select(OrderModel)
        .where(OrderModel.user_id == user_id)
        .order_by(OrderModel.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    orders = result.scalars().all()

    return [
        Order(
            order_id=order.order_id,
            user_id=order.user_id,
            status=order.status,
            total_amount=order.total_amount,
            created_at=order.created_at,
            items=[]
        )
        for order in orders
    ]


@router.get("/{order_id}", response_model=Order)
async def get_order(order_id: int, user_id: str, db: AsyncSession = Depends(get_db)):
    """Get order details."""
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
            detail="Order not found"
        )

    return Order(
        order_id=order.order_id,
        user_id=order.user_id,
        status=order.status,
        total_amount=order.total_amount,
        created_at=order.created_at,
        items=[]
    )


@router.post("/{order_id}/cancel", response_model=Order)
async def cancel_order(order_id: int, user_id: str, db: AsyncSession = Depends(get_db)):
    """Cancel order if possible."""
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
            detail="Order not found"
        )

    # Check if order can be cancelled
    cancellable_statuses = [OrderStatus.CREATED, OrderStatus.PAID]
    if order.status not in cancellable_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel order in {order.status} status"
        )

    # Release stock reservation
    result = await db.execute(
        select(OrderItemModel).where(OrderItemModel.order_id == order_id)
    )
    order_items = result.scalars().all()

    for item in order_items:
        try:
            await b2b_client.reserve_stock([
                {"sku_id": item.sku_id, "quantity": -item.quantity}
            ])
        except Exception:
            # Log but don't fail - manual intervention may be needed
            pass

    order.status = OrderStatus.CANCELLED
    await db.commit()
    await db.refresh(order)

    return Order(
        order_id=order.order_id,
        user_id=order.user_id,
        status=order.status,
        total_amount=order.total_amount,
        created_at=order.created_at,
        items=[]
    )
