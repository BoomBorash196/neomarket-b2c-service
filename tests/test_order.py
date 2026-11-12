"""Tests for checkout endpoint — DoD: 4 required pytest cases."""

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.models import OrderModel, OrderItemModel, CartItemModel
from src.schemas import OrderStatus
from src.services.b2b_client import B2BClientError, b2b_client


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


def _mock_product(product_id: str, title: str) -> dict:
    """B2B product-shaped response used for historical product names."""
    return {
        "id": product_id,
        "title": title,
        "status": "MODERATED",
        "seller_id": "seller-1",
        "category_id": "cat-1",
        "description": "",
        "images": [],
        "characteristics": [],
        "skus": [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
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
    products = {
        "prod-001": _mock_product("prod-001", "Widget Product A"),
        "prod-002": _mock_product("prod-002", "Widget Product B"),
    }
    b2b_client.get_product_by_id = AsyncMock(side_effect=lambda product_id: products[product_id])

    payload = {
        "user_id": user_id,
    }

    response = client.post("/api/v1/orders", json=payload, headers={"Idempotency-Key": idem_key})
    assert response.status_code == 201
    data = response.json()

    # B2B reserve receives the published ReserveRequest shape.
    reserve_kwargs = b2b_client.reserve_stock.await_args.kwargs
    assert reserve_kwargs["idempotency_key"] == idem_key
    assert reserve_kwargs["order_id"] == data["order_id"]
    assert reserve_kwargs["reservations"] == [
        {"sku_id": sku_1, "quantity": 2},
        {"sku_id": sku_2, "quantity": 1},
    ]

    # OpenAPI OrderResponse required fields
    for field in ("id", "buyer_id", "status", "items", "subtotal", "total", "address", "created_at"):
        assert field in data, f"missing required field {field}"
    assert data["id"] == data["order_id"]
    assert data["buyer_id"] == user_id
    assert data["subtotal"] == data["total"]
    for required in ("country", "city", "street", "building", "id", "created_at"):
        assert required in data["address"]
    for item in data["items"]:
        for required in ("sku_id", "product_id", "name", "quantity", "unit_price", "line_total"):
            assert required in item

    # Order created with PAID status
    assert data["user_id"] == user_id
    assert data["status"] == OrderStatus.PAID.value
    assert data["total_amount"] == 4500.0  # 1000*2 + 2500*1
    assert data["total"] == 4500
    assert len(data["items"]) == 2

    # Fixed snapshot prices in OrderItem
    prices = {it["sku_id"]: it["unit_price"] for it in data["items"]}
    assert prices[sku_1] == 1000.0
    assert prices[sku_2] == 2500.0

    # Historical data captured — product title comes from the product, not SKU name.
    titles = {it["sku_id"]: it["product_title"] for it in data["items"]}
    assert titles[sku_1] == "Widget Product A"
    assert titles[sku_2] == "Widget Product B"

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
    b2b_client.reserve_stock = AsyncMock(
        side_effect=B2BClientError(
            status_code=409,
            message="Insufficient stock",
            details={"failed_items": failed},
        )
    )
    b2b_client.get_product_by_id = AsyncMock(
        side_effect=lambda product_id: _mock_product(product_id, "Widget Product C")
    )

    payload = {"user_id": user_id}

    response = client.post("/api/v1/orders", json=payload, headers={"Idempotency-Key": idem_key})
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
    b2b_client.get_product_by_id = AsyncMock(
        side_effect=lambda product_id: _mock_product(product_id, "Widget Product D")
    )

    payload = {"user_id": user_id}

    # First call
    r1 = client.post("/api/v1/orders", json=payload, headers={"Idempotency-Key": idem_key})
    assert r1.status_code == 201
    order_id_1 = r1.json()["order_id"]

    # Second call with same key — should return the same order (idempotent)
    r2 = client.post("/api/v1/orders", json=payload, headers={"Idempotency-Key": idem_key})
    assert r2.status_code == 201
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

    payload = {"user_id": user_id}

    response = client.post("/api/v1/orders", json=payload, headers={"Idempotency-Key": "idem-503"})
    assert response.status_code == 503
    data = response.json()
    assert data["code"] == "B2B_UNAVAILABLE"

    # No order created
    result = await db_session.execute(
        select(OrderModel).where(OrderModel.user_id == user_id)
    )
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_b2b_product_card_unavailable_returns_503(
    client: TestClient, db_session
):
    """B2B failure while fetching the product card must map to 503, not 500."""
    user_id = "test_user_product_503"
    sku_id = "sku-product-503"

    db_session.add(_mock_cart_item(user_id, sku_id, quantity=1))
    await db_session.commit()

    b2b_client.get_skus_by_ids = AsyncMock(
        return_value={
            sku_id: _mock_sku(
                sku_id,
                price=100.0,
                product_id="prod-card-503",
            )
        }
    )
    b2b_client.get_product_by_id = AsyncMock(
        side_effect=B2BClientError(
            status_code=503,
            message="Product card service unavailable",
        )
    )
    b2b_client.reserve_stock = AsyncMock(return_value=_mock_reserve_success())

    response = client.post(
        "/api/v1/orders",
        json={"user_id": user_id},
        headers={"Idempotency-Key": "idem-product-card-503"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "B2B_UNAVAILABLE"

    # Enrichment happens before reserve, so nothing may be reserved or created.
    b2b_client.reserve_stock.assert_not_awaited()
    result = await db_session.execute(
        select(OrderModel).where(OrderModel.user_id == user_id)
    )
    assert result.scalars().all() == []


# =====================================================================
# 5. checkout_snapshot_mismatch_returns_cart_validation_response
# =====================================================================

@pytest.mark.asyncio
async def test_checkout_snapshot_mismatch_returns_cart_validation_response(
    client: TestClient, db_session
):
    """Snapshot mismatch → OpenAPI CartValidationResponse with 422."""
    user_id = "test_user_snapshot"
    sku_id = "sku-snapshot-001"

    db_session.add(_mock_cart_item(user_id, sku_id, quantity=1))
    await db_session.commit()

    b2b_client.get_skus_by_ids = AsyncMock(
        return_value={sku_id: _mock_sku(sku_id, price=500.0)}
    )
    b2b_client.get_product_by_id = AsyncMock(
        side_effect=lambda product_id: _mock_product(product_id, "Widget Product F")
    )

    response = client.post(
        "/api/v1/orders",
        json={
            "user_id": user_id,
            "items_snapshot": [
                {
                    "sku_id": sku_id,
                    "quantity": 2,
                    "unit_price": 500,
                }
            ],
        },
        headers={"Idempotency-Key": "idem-snapshot-001"},
    )

    assert response.status_code == 422
    data = response.json()
    assert data["is_valid"] is False
    assert data["cart"]["items"][0]["sku_id"] == sku_id
    assert data["cart"]["items"][0]["quantity"] == 1
    assert len(data["issues"]) == 1
    assert data["issues"][0]["sku_id"] == sku_id
    assert data["issues"][0]["type"] == "QUANTITY_REDUCED"


# =====================================================================
# 6. checkout_empty_cart_returns_400
# =====================================================================

@pytest.mark.asyncio
async def test_checkout_empty_cart_returns_400(
    client: TestClient, db_session
):
    """Empty cart → 400 with EMPTY_CART error."""
    user_id = "test_user_empty"

    payload = {"user_id": user_id}

    response = client.post("/api/v1/orders", json=payload, headers={"Idempotency-Key": "idem-empty"})
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

    response = client.get(f"/api/v1/orders/{order.order_id}?user_id={user_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["order_id"] == order.order_id
    assert data["total_amount"] == 3000.0
    assert len(data["items"]) == 1
    assert data["items"][0]["sku_name"] == "SKU g1"
    assert data["items"][0]["unit_price"] == 1000.0


# =====================================================================
# 7. idempotency_key_reused_with_different_body_returns_409
# =====================================================================

@pytest.mark.asyncio
async def test_idempotency_key_reused_with_different_body_returns_409(
    client: TestClient, db_session
):
    """Same key + different body → 409 IDEMPOTENCY_KEY_REUSED."""
    user_id = "test_user_idem_mismatch"
    sku_id = "sku-idem-mismatch"
    idem_key = "idem-mismatch-001"

    db_session.add(_mock_cart_item(user_id, sku_id, quantity=1))
    await db_session.commit()

    b2b_client.get_skus_by_ids = AsyncMock(
        return_value={sku_id: _mock_sku(sku_id, price=100.0)}
    )
    b2b_client.reserve_stock = AsyncMock(return_value=_mock_reserve_success())
    b2b_client.get_product_by_id = AsyncMock(
        side_effect=lambda product_id: _mock_product(product_id, "Widget Product E")
    )

    r1 = client.post(
        "/api/v1/orders",
        json={"user_id": user_id, "comment": "first"},
        headers={"Idempotency-Key": idem_key},
    )
    assert r1.status_code == 201

    r2 = client.post(
        "/api/v1/orders",
        json={"user_id": user_id, "comment": "second"},
        headers={"Idempotency-Key": idem_key},
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


# =====================================================================
# 8. get_orders_returns_paginated_history
# =====================================================================

@pytest.mark.asyncio
async def test_get_orders_returns_paginated_history(
    client: TestClient, db_session
):
    """GET /orders returns the OpenAPI PaginatedOrders envelope."""
    user_id = "test_user_history"
    order = OrderModel(user_id=user_id, status=OrderStatus.PAID, total_amount=100.0)
    db_session.add(order)
    await db_session.commit()

    response = client.get(
        "/api/v1/orders?limit=10&offset=0",
        headers={"X-User-Id": user_id},
    )
    assert response.status_code == 200
    data = response.json()
    for field in ("items", "total_count", "limit", "offset"):
        assert field in data
    assert data["total_count"] == 1
    assert data["limit"] == 10
    assert data["offset"] == 0
    assert data["items"][0]["buyer_id"] == user_id


# =====================================================================
# 9. checkout_without_identity_returns_401
# =====================================================================

@pytest.mark.asyncio
async def test_checkout_without_identity_returns_401(client: TestClient):
    """No X-User-Id and no legacy user_id → 401."""
    response = client.post(
        "/api/v1/orders",
        json={},
        headers={"Idempotency-Key": "idem-no-user"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"

