"""Tests for fulfill on delivery — US-ORD-05 / B2C-13."""

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.config import settings
from src.models import OrderModel, OrderItemModel
from src.schemas import OrderStatus
from src.services.b2b_client import b2b_client, B2BClientError

SERVICE_HEADERS = {"X-Service-Key": settings.B2B_SERVICE_KEY}


async def _create_delivering_order(db_session, user_id: str = "user-fulfill") -> OrderModel:
    order = OrderModel(
        user_id=user_id,
        status=OrderStatus.DELIVERING,
        total_amount=1500.0,
    )
    db_session.add(order)
    await db_session.flush()
    db_session.add(OrderItemModel(
        order_id=order.order_id,
        sku_id="sku-fulfill-1",
        sku_name="Fulfill Widget",
        product_id="prod-fulfill",
        product_title="Widget",
        quantity=2,
        unit_price=750.0,
    ))
    await db_session.commit()
    return order


# =====================================================================
# delivered_status_triggers_fulfill_to_b2b
# =====================================================================

@pytest.mark.asyncio
async def test_delivered_status_triggers_fulfill_to_b2b(
    client: TestClient, db_session
):
    """Happy path: DELIVERED transition calls B2B fulfill with order items."""
    order = await _create_delivering_order(db_session)

    b2b_client.fulfill_stock = AsyncMock(return_value={"order_id": order.order_id, "fulfilled": True})

    response = client.patch(
        f"/api/v1/orders/{order.order_id}/status",
        json={"status": OrderStatus.DELIVERED.value},
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == OrderStatus.DELIVERED.value

    b2b_client.fulfill_stock.assert_called_once()
    call_kwargs = b2b_client.fulfill_stock.call_args
    assert call_kwargs[0][0] == order.order_id
    reservations = call_kwargs[0][1]
    assert len(reservations) == 1
    assert reservations[0]["sku_id"] == "sku-fulfill-1"
    assert reservations[0]["quantity"] == 2
    assert reservations[0]["order_id"] == order.order_id

    result = await db_session.execute(
        select(OrderModel).where(OrderModel.order_id == order.order_id)
    )
    refreshed = result.scalar_one()
    assert refreshed.fulfill_completed is True


# =====================================================================
# fulfill_failure_retried_asynchronously
# =====================================================================

@pytest.mark.asyncio
async def test_fulfill_failure_retried_asynchronously(
    client: TestClient, db_session
):
    """B2B fulfill fails → order stays DELIVERED, retry runs in background."""
    order = await _create_delivering_order(db_session, user_id="user-fulfill-retry")
    order_id = order.order_id

    fulfill_mock = AsyncMock(
        side_effect=[
            B2BClientError(status_code=503, message="B2B unavailable"),
            {"order_id": order_id, "fulfilled": True},
        ]
    )
    b2b_client.fulfill_stock = fulfill_mock

    response = client.patch(
        f"/api/v1/orders/{order_id}/status",
        json={"status": OrderStatus.DELIVERED.value},
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == OrderStatus.DELIVERED.value

    # TestClient runs BackgroundTasks after response — initial + async retry
    assert fulfill_mock.call_count == 2

    from tests.conftest import AsyncSessionLocal

    async with AsyncSessionLocal() as check_db:
        result = await check_db.execute(
            select(OrderModel).where(OrderModel.order_id == order_id)
        )
        refreshed = result.scalar_one()
    assert refreshed.status == OrderStatus.DELIVERED
    assert refreshed.fulfill_completed is True


# =====================================================================
# repeated_fulfill_idempotent
# =====================================================================

@pytest.mark.asyncio
async def test_repeated_fulfill_idempotent(
    client: TestClient, db_session
):
    """Repeat fulfill with same order_id → 200, B2B called once (client idempotency)."""
    order = await _create_delivering_order(db_session, user_id="user-fulfill-idem")

    fulfill_mock = AsyncMock(return_value={"order_id": order.order_id, "fulfilled": True})
    b2b_client.fulfill_stock = fulfill_mock

    client.patch(
        f"/api/v1/orders/{order.order_id}/status",
        json={"status": OrderStatus.DELIVERED.value},
        headers=SERVICE_HEADERS,
    )

    retry_response = client.post(
        f"/api/v1/orders/{order.order_id}/fulfill-retry",
        headers=SERVICE_HEADERS,
    )
    assert retry_response.status_code == 200
    assert retry_response.json()["status"] == OrderStatus.DELIVERED.value

    # fulfill_completed=True skips second B2B call
    fulfill_mock.assert_called_once()
