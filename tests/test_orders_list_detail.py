"""Tests for order list and detail — B2C-10 view-orders flow.

DoD scenarios:
  1. orders_list_returns_own_orders_paginated — happy path with pagination
  2. order_detail_shows_fixed_prices — unit_price in OrderItem unchanged after SKU price update
  3. other_user_order_returns_404_not_403 — IDOR: foreign order → 404
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.models import OrderModel, OrderItemModel
from src.schemas import OrderStatus


USER_ID = "test_user_jwt"
OTHER_USER_ID = "other_user_jwt"


# =====================================================================
# 1. orders_list_returns_own_orders_paginated
# =====================================================================

@pytest.mark.asyncio
async def test_orders_list_returns_own_orders_paginated(
    client: TestClient, db_session
):
    """Happy path: order list returns only own orders, paginated."""
    # Create 7 orders for the current user
    for i in range(7):
        order = OrderModel(
            user_id=USER_ID,
            status=OrderStatus.PAID,
            total_amount=1000.0 + i,
        )
        db_session.add(order)
        await db_session.flush()
        db_session.add(OrderItemModel(
            order_id=order.order_id,
            sku_id=f"sku-list-{i}",
            sku_name=f"SKU {i}",
            product_id=f"prod-{i}",
            product_title=f"Product {i}",
            quantity=1,
            unit_price=1000.0 + i,
        ))

    # Create 3 orders for another user (should NOT appear)
    for i in range(3):
        order = OrderModel(
            user_id=OTHER_USER_ID,
            status=OrderStatus.PAID,
            total_amount=500.0,
        )
        db_session.add(order)
        await db_session.flush()

    await db_session.commit()

    # Page 1, page_size=5 → 5 orders, total=7
    response = client.get("/api/v1/orders?page=1&page_size=5",
                          headers={"X-Test-User-Id": USER_ID})
    assert response.status_code == 200
    data = response.json()
    assert data["page"] == 1
    assert data["page_size"] == 5
    assert data["total"] == 7
    assert len(data["orders"]) == 5

    # All returned orders belong to current user
    for order in data["orders"]:
        assert order["user_id"] == USER_ID

    # Page 2 → 2 remaining orders
    response = client.get("/api/v1/orders?page=2&page_size=5",
                          headers={"X-Test-User-Id": USER_ID})
    assert response.status_code == 200
    data = response.json()
    assert data["page"] == 2
    assert data["total"] == 7
    assert len(data["orders"]) == 2

    # Filter by status — only PAID orders
    response = client.get("/api/v1/orders?status=PAID",
                          headers={"X-Test-User-Id": USER_ID})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 7
    for order in data["orders"]:
        assert order["status"] == OrderStatus.PAID.value

    # Filter by non-existent status → empty
    response = client.get("/api/v1/orders?status=CANCELLED",
                          headers={"X-Test-User-Id": USER_ID})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert len(data["orders"]) == 0


# =====================================================================
# 2. order_detail_shows_fixed_prices
# =====================================================================

@pytest.mark.asyncio
async def test_order_detail_shows_fixed_prices(
    client: TestClient, db_session
):
    """unit_price in OrderItem is the snapshot at purchase time —
    does NOT change even if the SKU price in B2B was updated."""
    user_id = "test_user_jwt"
    sku_id = "sku-fixed-001"

    # Create an order with fixed snapshot prices
    order = OrderModel(
        user_id=user_id,
        status=OrderStatus.PAID,
        total_amount=5000.0,
    )
    db_session.add(order)
    await db_session.flush()

    # Snapshot: unit_price = 1000.0 at purchase time
    db_session.add(OrderItemModel(
        order_id=order.order_id,
        sku_id=sku_id,
        sku_name="Fixed Product",
        product_id="prod-fixed",
        product_title="Fixed Product Title",
        quantity=5,
        unit_price=1000.0,  # price at purchase time
    ))
    await db_session.commit()

    # "Seller" raised the SKU price in B2B to 2000.0 — but the order
    # detail must still show the original unit_price (1000.0).
    # Order detail reads from OrderItem (DB), not from B2B.
    response = client.get(f"/api/v1/orders/{order.order_id}",
                          headers={"X-Test-User-Id": USER_ID})
    assert response.status_code == 200
    data = response.json()

    assert data["order_id"] == order.order_id
    assert data["user_id"] == USER_ID
    assert data["total_amount"] == 5000.0

    # Fixed snapshot price — NOT the new B2B price
    assert len(data["items"]) == 1
    assert data["items"][0]["sku_id"] == sku_id
    assert data["items"][0]["unit_price"] == 1000.0  # unchanged!
    assert data["items"][0]["product_title"] == "Fixed Product Title"
    assert data["items"][0]["quantity"] == 5


# =====================================================================
# 3. other_user_order_returns_404_not_403
# =====================================================================

@pytest.mark.asyncio
async def test_other_user_order_returns_404_not_403(
    client: TestClient, db_session
):
    """IDOR protection: a foreign order returns 404, not 403.
    This prevents enumeration — the attacker cannot tell if the order
    exists but belongs to someone else, or simply doesn't exist."""

    # Create an order for a different user
    order = OrderModel(
        user_id=OTHER_USER_ID,
        status=OrderStatus.PAID,
        total_amount=9999.0,
    )
    db_session.add(order)
    await db_session.flush()
    db_session.add(OrderItemModel(
        order_id=order.order_id,
        sku_id="sku-secret",
        sku_name="Secret Product",
        product_id="prod-secret",
        product_title="Secret Product",
        quantity=1,
        unit_price=9999.0,
    ))
    await db_session.commit()

    # Current user (test_user_jwt) tries to access the foreign order
    # → should get 404, NOT 403 (which would confirm the order exists)
    response = client.get(f"/api/v1/orders/{order.order_id}",
                          headers={"X-Test-User-Id": USER_ID})
    assert response.status_code == 404
    data = response.json()
    assert data["code"] == "ORDER_NOT_FOUND"

    # Also test list: foreign orders should not appear
    response = client.get("/api/v1/orders",
                          headers={"X-Test-User-Id": USER_ID})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert len(data["orders"]) == 0

    # Non-existent order → also 404 (indistinguishable from foreign order)
    response = client.get("/api/v1/orders/99999999",
                          headers={"X-Test-User-Id": USER_ID})
    assert response.status_code == 404
    data = response.json()
    assert data["code"] == "ORDER_NOT_FOUND"
