"""Tests for cancel order endpoint — US-ORD-03."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.models import OrderItemModel, OrderModel
from src.schemas import OrderStatus
from src.services.b2b_client import b2b_client, B2BClientError
from src.services.order_cancel import backoff_delay, process_cancel_retries


async def _add_order_with_item(
    db_session,
    user_id: str,
    status: OrderStatus,
    *,
    sku_id: str = "sku-001",
    quantity: int = 1,
    unit_price: float = 1000.0,
    total_amount: float = 1000.0,
) -> OrderModel:
    order = OrderModel(
        user_id=user_id,
        status=status,
        total_amount=total_amount,
    )
    db_session.add(order)
    await db_session.flush()
    db_session.add(
        OrderItemModel(
            order_id=order.order_id,
            sku_id=sku_id,
            sku_name="Widget",
            product_id="prod-001",
            product_title="Widget A",
            quantity=quantity,
            unit_price=unit_price,
        )
    )
    return order


# =====================================================================
# 1. cancel_paid_order_transitions_to_cancelled
# =====================================================================

@pytest.mark.asyncio
async def test_cancel_paid_order_transitions_to_cancelled(
    client: TestClient, db_session
):
    """Happy path: PAID → CANCEL_PENDING → CANCELLED, unreserve called once."""
    user_id = "test_user_cancel_ok"
    order = await _add_order_with_item(
        db_session, user_id, OrderStatus.PAID, quantity=3, total_amount=3000.0
    )
    await db_session.commit()

    b2b_client.unreserve_order = AsyncMock(
        return_value={
            "order_id": order.order_id,
            "status": "UNRESERVED",
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel?user_id={user_id}",
        json={"reason": "changed my mind"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == OrderStatus.CANCELLED.value
    assert data["cancel_reason"] == "changed my mind"
    # OpenAPI OrderResponse required fields are still present on cancel.
    for field in (
        "id",
        "buyer_id",
        "status",
        "items",
        "subtotal",
        "total",
        "address",
        "created_at",
    ):
        assert field in data, f"missing required field {field}"

    b2b_client.unreserve_order.assert_awaited_once_with(
        order.order_id,
        [{"sku_id": "sku-001", "quantity": 3}],
    )

    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert order.cancel_retry_count == 0
    assert order.cancel_next_retry_at is None
    assert order.cancel_last_error is None


# =====================================================================
# 2. unreserve_failure_transitions_to_cancel_pending
# =====================================================================

@pytest.mark.asyncio
async def test_unreserve_failure_transitions_to_cancel_pending(
    client: TestClient, db_session
):
    """B2B unreserve fails → CANCEL_PENDING plus a scheduled retry."""
    user_id = "test_user_cancel_fail"
    order = await _add_order_with_item(
        db_session,
        user_id,
        OrderStatus.PAID,
        sku_id="sku-fail-001",
        quantity=2,
        unit_price=2500.0,
        total_amount=5000.0,
    )
    await db_session.commit()

    b2b_client.unreserve_order = AsyncMock(
        side_effect=B2BClientError(status_code=503, message="B2B unavailable")
    )

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel?user_id={user_id}"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == OrderStatus.CANCEL_PENDING.value

    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCEL_PENDING
    assert order.cancel_retry_count == 1
    assert order.cancel_next_retry_at is not None
    assert "B2B unavailable" in (order.cancel_last_error or "")


# =====================================================================
# 3. cancel_assembling_order_returns_409
# =====================================================================

@pytest.mark.asyncio
async def test_cancel_assembling_order_returns_409(
    client: TestClient, db_session
):
    """Order in ASSEMBLING cannot be cancelled → 409 CANCEL_NOT_ALLOWED."""
    user_id = "test_user_cancel_assembling"
    order = await _add_order_with_item(
        db_session, user_id, OrderStatus.ASSEMBLING, total_amount=1000.0
    )
    await db_session.commit()

    b2b_client.unreserve_order = AsyncMock()

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel?user_id={user_id}"
    )
    assert response.status_code == 409
    data = response.json()
    assert data["code"] == "CANCEL_NOT_ALLOWED"
    assert data["current_status"] == "ASSEMBLING"
    b2b_client.unreserve_order.assert_not_called()


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

    order = await _add_order_with_item(
        db_session, user_b, OrderStatus.CREATED, total_amount=100.0
    )
    await db_session.commit()

    b2b_client.unreserve_order = AsyncMock()

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel?user_id={user_a}"
    )
    assert response.status_code == 404
    data = response.json()
    assert data["code"] == "ORDER_NOT_FOUND"
    b2b_client.unreserve_order.assert_not_called()


# =====================================================================
# 5. cancel_retry_success
# =====================================================================

@pytest.mark.asyncio
async def test_cancel_retry_success(
    client: TestClient, db_session
):
    """POST /cancel-retry succeeds → order transitions to CANCELLED."""
    user_id = "test_user_retry"
    order = await _add_order_with_item(
        db_session,
        user_id,
        OrderStatus.CANCEL_PENDING,
        sku_id="sku-retry",
        total_amount=2000.0,
    )
    await db_session.commit()

    b2b_client.unreserve_order = AsyncMock(
        return_value={"order_id": order.order_id, "status": "UNRESERVED"}
    )

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel-retry?user_id={user_id}"
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
    order = await _add_order_with_item(
        db_session, user_id, OrderStatus.CREATED, total_amount=100.0
    )
    await db_session.commit()

    response = client.post(
        f"/api/v1/orders/{order.order_id}/cancel-retry?user_id={user_id}"
    )
    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "NOT_CANCEL_PENDING"


# =====================================================================
# 7. retry_worker_processes_due_cancel_pending_orders
# =====================================================================

@pytest.mark.asyncio
async def test_retry_worker_processes_due_cancel_pending_orders(db_session):
    """Cron worker cancels due CANCEL_PENDING orders and skips future ones."""
    user_id = "test_user_worker"

    due_one = await _add_order_with_item(
        db_session, user_id, OrderStatus.CANCEL_PENDING, sku_id="sku-due-1"
    )
    due_two = await _add_order_with_item(
        db_session, user_id, OrderStatus.CANCEL_PENDING, sku_id="sku-due-2"
    )
    future = await _add_order_with_item(
        db_session, user_id, OrderStatus.CANCEL_PENDING, sku_id="sku-future"
    )
    future.cancel_next_retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
    await db_session.commit()

    b2b_client.unreserve_order = AsyncMock(
        return_value={"status": "UNRESERVED"}
    )

    processed = await process_cancel_retries(db_session, limit=10)
    assert processed == 2
    assert b2b_client.unreserve_order.await_count == 2

    result = await db_session.execute(
        select(OrderModel).order_by(OrderModel.order_id)
    )
    statuses = {order.order_id: order.status for order in result.scalars()}
    assert statuses[due_one.order_id] == OrderStatus.CANCELLED
    assert statuses[due_two.order_id] == OrderStatus.CANCELLED
    assert statuses[future.order_id] == OrderStatus.CANCEL_PENDING


# =====================================================================
# 8. backoff grows exponentially and is capped
# =====================================================================

def test_cancel_retry_backoff_is_exponential_and_capped():
    assert backoff_delay(1) == 60
    assert backoff_delay(2) == 120
    assert backoff_delay(3) == 240
    assert backoff_delay(20) == 3600
