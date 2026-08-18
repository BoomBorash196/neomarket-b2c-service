"""Tests for cancel order endpoint — US-ORD-03."""

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.models import OrderModel, OrderItemModel, CartItemModel
from src.schemas import OrderStatus
from src.services.b2b_client import b2b_client, B2BClientError


# =====================================================================
# 1. cancel_paid_order_transitions_to_cancelled
# =====================================================================

@pytest.mark.asyncio
async def test_cancel_paid_order_transitions_to_cancelled(
    client: TestClient, db_session
):
    """Happy path: order in PAID → CANCELLED, unreserve called."""
    user_id = "test_user_cancel_ok"
    order = OrderModel(
        user_id=user_id,
        status=OrderStatus.PAID,
        total_amount=3000.0,
    )
    db_session.add(order)
    await db_session.flush()

    db_session.add(OrderItemModel(
        order_id=order.order_id,
        sku_id="sku-001",
        sku_name="Widget",
        product_id="prod-001",
        product_title="Widget A",
        quantity=3,
        unit_price=1000.0,
    ))
    await db_session.commit()

    # unreserve succeeds (negative quantity = release)
    b2b_client.reserve_stock = AsyncMock(return_value={"success": [{"sku_id": "sku-001", "reserved": -3, "remaining": 12}]})

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel",
        headers={"X-Test-User-Id": user_id}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == OrderStatus.CANCELLED.value

    # Verify unreserve was called
    b2b_client.reserve_stock.assert_called_once()
    call_args = b2b_client.reserve_stock.call_args
    reservations = call_args[0][0]
    assert len(reservations) == 1
    assert reservations[0]["sku_id"] == "sku-001"
    assert reservations[0]["quantity"] == -3  # negative = release


# =====================================================================
# 2. unreserve_failure_transitions_to_cancel_pending
# =====================================================================

@pytest.mark.asyncio
async def test_unreserve_failure_transitions_to_cancel_pending(
    client: TestClient, db_session
):
    """B2B unreserve fails → order transitions to CANCEL_PENDING."""
    user_id = "test_user_cancel_fail"
    order = OrderModel(
        user_id=user_id,
        status=OrderStatus.PAID,
        total_amount=5000.0,
    )
    db_session.add(order)
    await db_session.flush()

    db_session.add(OrderItemModel(
        order_id=order.order_id,
        sku_id="sku-fail-001",
        sku_name="Gadget",
        product_id="prod-fail",
        product_title="Gadget X",
        quantity=2,
        unit_price=2500.0,
    ))
    await db_session.commit()

    # unreserve fails
    b2b_client.reserve_stock.reset_mock()
    b2b_client.reserve_stock = AsyncMock(
        side_effect=B2BClientError(status_code=503, message="B2B unavailable")
    )

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel",
        headers={"X-Test-User-Id": user_id}
    )
    assert response.status_code == 200
    data = response.json()

    # Order transitions to CANCEL_PENDING (not left in PAID)
    assert data["status"] == OrderStatus.CANCEL_PENDING.value
    assert b2b_client.reserve_stock.called


# =====================================================================
# 3. cancel_assembling_order_returns_409
# =====================================================================

@pytest.mark.asyncio
async def test_cancel_assembling_order_returns_409(
    client: TestClient, db_session
):
    """Order in DELIVERING cannot be cancelled → 409 CANCEL_NOT_ALLOWED."""
    user_id = "test_user_cancel_delivering"
    order = OrderModel(
        user_id=user_id,
        status=OrderStatus.DELIVERING,
        total_amount=1000.0,
    )
    db_session.add(order)
    await db_session.commit()

    # Reset mock state before this test
    b2b_client.reserve_stock.reset_mock()

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel",
        headers={"X-Test-User-Id": user_id}
    )
    assert response.status_code == 409
    data = response.json()
    assert data["code"] == "CANCEL_NOT_ALLOWED"
    assert data["current_status"] == "DELIVERING"

    # unreserve NOT called
    b2b_client.reserve_stock.assert_not_called()


# =====================================================================
# 4. other_user_order_returns_404
# =====================================================================

@pytest.mark.asyncio
async def test_other_user_order_returns_404(
    client: TestClient, db_session
):
    """IDOR: user trying to cancel another user's order → 404."""
    user_a = "user-a"
    user_b = "user-b"

    order = OrderModel(
        user_id=user_b,
        status=OrderStatus.CREATED,
        total_amount=100.0,
    )
    db_session.add(order)
    await db_session.commit()

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel",
        headers={"X-Test-User-Id": user_a}
    )
    assert response.status_code == 404
    data = response.json()
    assert data["code"] == "ORDER_NOT_FOUND"


# =====================================================================
# 5. cancel_retry_success
# =====================================================================

@pytest.mark.asyncio
async def test_cancel_retry_success(
    client: TestClient, db_session
):
    """POST /cancel-retry succeeds → order transitions to CANCELLED."""
    user_id = "test_user_retry"
    order = OrderModel(
        user_id=user_id,
        status=OrderStatus.CANCEL_PENDING,
        total_amount=2000.0,
    )
    db_session.add(order)
    await db_session.flush()

    db_session.add(OrderItemModel(
        order_id=order.order_id,
        sku_id="sku-retry",
        sku_name="Retry Item",
        product_id="prod-retry",
        product_title="Retry Product",
        quantity=1,
        unit_price=2000.0,
    ))
    await db_session.commit()

    b2b_client.reserve_stock = AsyncMock(return_value={"success": [{"sku_id": "sku-retry", "reserved": -1, "remaining": 10}]})

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel-retry",
        headers={"X-Test-User-Id": user_id}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == OrderStatus.CANCELLED.value


# =====================================================================
# 6. cancel_retry_wrong_status_returns_400
# =====================================================================

@pytest.mark.asyncio
async def test_cancel_retry_wrong_status_returns_400(
    client: TestClient, db_session
):
    """Cancel retry on non-CANCEL_PENDING order → 400."""
    user_id = "test_user_retry_bad"
    order = OrderModel(
        user_id=user_id,
        status=OrderStatus.CREATED,
        total_amount=100.0,
    )
    db_session.add(order)
    await db_session.commit()

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel-retry",
        headers={"X-Test-User-Id": user_id}
    )
    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "NOT_CANCEL_PENDING"