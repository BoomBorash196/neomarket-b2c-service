"""Tests for checkout endpoint — DoD: 4 required pytest cases."""

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.models import OrderModel, OrderItemModel, CartItemModel
from src.schemas import OrderStatus
from src.services.b2b_client import b2b_client


# =====================================================================
# Shared mocks
# =====================================================================

def _mock_cart_item(user_id: str, sku_id: str, quantity: int = 1):
    """Create a cart item for test setup."""
    return CartItemModel(user_id=user_id, sku_id=sku_id, quantity=quantity)


def _mock_sku(sku_id: str, price: float = 1000.0,
              quantity_available: int = 10, is_active: bool = True,
              product_id: str = "prod-001", name: str = "Test Product") -> dict:
    return {
        "sku_id": sku_id,
        "product_id": product_id,
        "name": name,
        "price": price,
        "quantity_available": quantity_available,
        "is_active": is_active,
    }


def _mock_reserve_success(failed: list = None) -> dict:
    """B2B /inventory/reserve returns {"success": [...], "failed": [...]}."""
    return {
        "success": [{"sku_id": "x", "reserved": 1, "remaining": 9}] if failed is None else [],
        "failed": failed or [],
        "total_reserved": 1 if failed is None else 0,
        "total_failed": len(failed or []),
    }


# =====================================================================
# 1. checkout_creates_paid_order_with_fixed_prices
# =====================================================================

@pytest.mark.asyncio
async def test_checkout_creates_paid_order_with_fixed_prices(
    client: TestClient, db_session
):
    """Happy path: order created with PAID status and fixed snapshot prices in OrderItem."""
    user_id = "test_user_checkout"
    sku_1 = "sku-chk-001"
    sku_2 = "sku-chk-002"
    idem_key = "idem-chk-001"

    # Add items to cart
    db_session.add(_mock_cart_item(user_id, sku_1, quantity=2))
    db_session.add(_mock_cart_item(user_id, sku_2, quantity=1))
    await db_session.commit()

    skus_data = {
        sku_1: _mock_sku(sku_1, price=1000.0, product_id="prod-001", name="Widget A"),
        sku_2: _mock_sku(sku_2, price=2500.0, product_id="prod-002", name="Widget B"),
    }

    b2b_client.get_skus_by_ids = AsyncMock(return_value=skus_data)
    b2b_client.reserve_stock = AsyncMock(return_value=_mock_reserve_success())

    # user_id now comes from JWT (X-Test-User-Id header), not body
    response = client.post(
        "/api/v1/orders",
        json={},
        headers={"Idempotency-Key": idem_key, "X-Test-User-Id": user_id},
    )
    assert response.status_code == 200
    data = response.json()

    # Order created with PAID status
    assert data["user_id"] == user_id
    assert data["status"] == OrderStatus.PAID.value
    assert data["total_amount"] == 4500.0  # 1000*2 + 2500*1
    assert len(data["items"]) == 2

    # Fixed snapshot prices in OrderItem
    prices = {it["sku_id"]: it["unit_price"] for it in data["items"]}
    assert prices[sku_1] == 1000.0
    assert prices[sku_2] == 2500.0

    # Historical data captured
    titles = {it["sku_id"]: it["product_title"] for it in data["items"]}
    assert titles[sku_1] == "Widget A"
    assert titles[sku_2] == "Widget B"

    # Idempotency key stored
    result = await db_session.execute(
        select(OrderModel).where(OrderModel.idempotency_key == idem_key)
    )
    assert result.scalar_one() is not None

    # Cart cleared
    cart_result = await db_session.execute(
        select(CartItemModel).where(CartItemModel.user_id == user_id)
    )
    assert cart_result.scalars().all() == []


# =====================================================================
# 2. partial_reserve_failure_returns_409
# =====================================================================

@pytest.mark.asyncio
async def test_partial_reserve_failure_returns_409(
    client: TestClient, db_session
):
    """If at least one SKU fails to reserve, all is rolled back → 409."""
    user_id = "test_user_fail"
    sku_ok = "sku-ok-001"
    sku_fail = "sku-fail-001"
    idem_key = "idem-fail-001"

    db_session.add(_mock_cart_item(user_id, sku_ok, quantity=1))
    db_session.add(_mock_cart_item(user_id, sku_fail, quantity=1))
    await db_session.commit()

    skus_data = {
        sku_ok: _mock_sku(sku_ok, price=100.0),
        sku_fail: _mock_sku(sku_fail, price=200.0),
    }

    failed = [{"sku_id": sku_fail, "reason": "out_of_stock"}]
    b2b_client.get_skus_by_ids = AsyncMock(return_value=skus_data)
    b2b_client.reserve_stock = AsyncMock(return_value=_mock_reserve_success(failed=failed))

    response = client.post(
        "/api/v1/orders",
        json={},
        headers={"Idempotency-Key": idem_key, "X-Test-User-Id": user_id},
    )
    assert response.status_code == 409
    data = response.json()
    assert data["code"] == "RESERVE_FAILED"
    assert len(data.get("failed_items", [])) == 1
    assert data["failed_items"][0]["sku_id"] == sku_fail

    # No order created — all-or-nothing
    result = await db_session.execute(
        select(OrderModel).where(OrderModel.user_id == user_id)
    )
    assert result.scalars().all() == []


# =====================================================================
# 3. idempotency_returns_existing_order
# =====================================================================

@pytest.mark.asyncio
async def test_idempotency_returns_existing_order(
    client: TestClient, db_session
):
    """Second POST with same idempotency_key returns the existing order."""
    user_id = "test_user_idem"
    sku_id = "sku-idem-001"
    idem_key = "idem-dup-001"

    db_session.add(_mock_cart_item(user_id, sku_id, quantity=1))
    await db_session.commit()

    skus_data = {sku_id: _mock_sku(sku_id, price=500.0)}
    b2b_client.get_skus_by_ids = AsyncMock(return_value=skus_data)
    b2b_client.reserve_stock = AsyncMock(return_value=_mock_reserve_success())

    # First call
    r1 = client.post(
        "/api/v1/orders",
        json={},
        headers={"Idempotency-Key": idem_key, "X-Test-User-Id": user_id},
    )
    assert r1.status_code == 200
    order_id_1 = r1.json()["order_id"]

    # Second call with same key — should return the same order (idempotent)
    r2 = client.post(
        "/api/v1/orders",
        json={},
        headers={"Idempotency-Key": idem_key, "X-Test-User-Id": user_id},
    )
    assert r2.status_code == 200
    order_id_2 = r2.json()["order_id"]

    # Same order returned — key proof of idempotency
    assert order_id_1 == order_id_2

    # The response items match — confirms it's the same order data
    items_1 = {it["sku_id"]: it["unit_price"] for it in r1.json()["items"]}
    items_2 = {it["sku_id"]: it["unit_price"] for it in r2.json()["items"]}
    assert items_1 == items_2


# =====================================================================
# 4. b2b_unavailable_returns_503
# =====================================================================

@pytest.mark.asyncio
async def test_b2b_unavailable_returns_503(
    client: TestClient, db_session
):
    """B2B unavailable → 503 Service Unavailable."""
    user_id = "test_user_503"
    sku_id = "sku-503-001"

    db_session.add(_mock_cart_item(user_id, sku_id, quantity=1))
    await db_session.commit()

    from src.services.b2b_client import B2BClientError
    b2b_client.get_skus_by_ids = AsyncMock(
        side_effect=B2BClientError(status_code=503, message="Service unavailable")
    )

    response = client.post(
        "/api/v1/orders",
        json={},
        headers={"Idempotency-Key": "idem-503", "X-Test-User-Id": user_id},
    )
    assert response.status_code == 503
    data = response.json()
    assert data["code"] == "B2B_UNAVAILABLE"

    # No order created
    result = await db_session.execute(
        select(OrderModel).where(OrderModel.user_id == user_id)
    )
    assert result.scalars().all() == []


# =====================================================================
# 5. checkout_empty_cart_returns_400
# =====================================================================

@pytest.mark.asyncio
async def test_checkout_empty_cart_returns_400(
    client: TestClient, db_session
):
    """Empty cart → 400 with EMPTY_CART error."""
    user_id = "test_user_empty"

    response = client.post(
        "/api/v1/orders",
        json={},
        headers={"Idempotency-Key": "idem-empty", "X-Test-User-Id": user_id},
    )
    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "EMPTY_CART"


# =====================================================================
# 6. get_order_with_items
# =====================================================================

@pytest.mark.asyncio
async def test_get_order_with_items(
    client: TestClient, db_session
):
    """GET /orders/{id} returns order with items."""
    user_id = "test_user_get"

    # Create order directly
    order = OrderModel(user_id=user_id, status=OrderStatus.PAID, total_amount=3000.0)
    db_session.add(order)
    await db_session.flush()

    db_session.add(OrderItemModel(
        order_id=order.order_id, sku_id="sku-g1", sku_name="SKU g1",
        product_id="prod-g1", product_title="Get Item", quantity=3, unit_price=1000.0
    ))
    await db_session.commit()

    # user_id from JWT (X-Test-User-Id header), not query
    response = client.get(
        f"/api/v1/orders/{order.order_id}",
        headers={"X-Test-User-Id": user_id},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["order_id"] == order.order_id
    assert data["total_amount"] == 3000.0
    assert len(data["items"]) == 1
    assert data["items"][0]["sku_name"] == "SKU g1"
    assert data["items"][0]["unit_price"] == 1000.0
