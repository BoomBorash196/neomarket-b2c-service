"""Tests for product availability change subscriptions — DoD: 4 required pytest cases."""

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.models import SubscriptionModel
from src.services.b2b_client import b2b_client


# =====================================================================
# Shared mocks
# =====================================================================

def _mock_sku_ok(
    sku_id: str = "sku-001",
    price: float = 1000.0,
    quantity_available: int = 10,
    is_active: bool = True,
    product_id: str = "prod-001",
    product_title: str = "Test Product",
) -> dict:
    """Return a valid B2B SKU response."""
    return {
        "sku_id": sku_id,
        "product_id": product_id,
        "product_title": product_title,
        "price": price,
        "quantity_available": quantity_available,
        "is_active": is_active,
    }


# =====================================================================
# 1. subscribe_returns_201_with_notify_on — happy path
# =====================================================================

@pytest.mark.asyncio
async def test_subscribe_returns_201_with_notify_on(
    client: TestClient, db_session
):
    """Happy path: subscribing to a valid SKU returns 201 with correct payload."""
    user_id = "sub-user-001"
    sku_id = "sku-sub-001"
    mock_data = _mock_sku_ok(sku_id=sku_id, price=500.0)

    b2b_client.get_sku_by_id = AsyncMock(return_value=mock_data)

    response = client.post(
        "/api/v1/cart/subscribe",
        json={"sku_id": sku_id, "notify_on": "in_stock"},
        headers={"X-User-Id": user_id},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["sku_id"] == sku_id
    assert data["notify_on"] == "in_stock"
    assert "subscription_id" in data
    assert "created_at" in data

    # Verify persisted in DB
    result = await db_session.execute(
        select(SubscriptionModel).where(
            (SubscriptionModel.user_id == user_id) &
            (SubscriptionModel.sku_id == sku_id)
        )
    )
    sub = result.scalar_one_or_none()
    assert sub is not None
    assert sub.notify_on == "in_stock"


# =====================================================================
# 2. duplicate_subscription_returns_409
# =====================================================================

@pytest.mark.asyncio
async def test_duplicate_subscription_returns_409(
    client: TestClient, db_session
):
    """Second subscription to the same SKU → 409 Conflict."""
    user_id = "sub-user-002"
    sku_id = "sku-sub-002"
    mock_data = _mock_sku_ok(sku_id=sku_id, price=300.0)

    b2b_client.get_sku_by_id = AsyncMock(return_value=mock_data)

    # First subscription — should succeed
    response = client.post(
        "/api/v1/cart/subscribe",
        json={"sku_id": sku_id, "notify_on": "in_stock"},
        headers={"X-User-Id": user_id},
    )
    assert response.status_code == 201

    # Second subscription to the same SKU — should fail with 409
    response = client.post(
        "/api/v1/cart/subscribe",
        json={"sku_id": sku_id, "notify_on": "in_stock"},
        headers={"X-User-Id": user_id},
    )
    assert response.status_code == 409
    data = response.json()
    assert data["code"] == "DUPLICATE_SUBSCRIPTION"


# =====================================================================
# 3. invalid_notify_on_returns_400
# =====================================================================

@pytest.mark.asyncio
async def test_invalid_notify_on_returns_400(
    client: TestClient, db_session
):
    """Empty or invalid notify_on → 400 Bad Request."""
    user_id = "sub-user-003"
    sku_id = "sku-sub-003"
    mock_data = _mock_sku_ok(sku_id=sku_id, price=200.0)

    b2b_client.get_sku_by_id = AsyncMock(return_value=mock_data)

    # Empty notify_on — Pydantic will reject it
    response = client.post(
        "/api/v1/cart/subscribe",
        json={"sku_id": sku_id, "notify_on": ""},
        headers={"X-User-Id": user_id},
    )
    assert response.status_code == 422  # Pydantic validation error

    # Invalid notify_on value
    response = client.post(
        "/api/v1/cart/subscribe",
        json={"sku_id": sku_id, "notify_on": "invalid_event"},
        headers={"X-User-Id": user_id},
    )
    assert response.status_code == 422  # Pydantic validation error


# =====================================================================
# 4. subscribe_to_unknown_product_returns_404
# =====================================================================

@pytest.mark.asyncio
async def test_subscribe_to_unknown_product_returns_404(
    client: TestClient, db_session
):
    """Subscribing to a non-existent SKU → 404 Not Found."""
    user_id = "sub-user-004"
    sku_id = "sku-nonexistent"

    b2b_client.get_sku_by_id = AsyncMock(return_value=None)

    response = client.post(
        "/api/v1/cart/subscribe",
        json={"sku_id": sku_id, "notify_on": "in_stock"},
        headers={"X-User-Id": user_id},
    )

    assert response.status_code == 404
    data = response.json()
    assert data["code"] == "SKU_NOT_FOUND"

    # Verify no subscription was created
    result = await db_session.execute(
        select(SubscriptionModel).where(
            (SubscriptionModel.user_id == user_id) &
            (SubscriptionModel.sku_id == sku_id)
        )
    )
    assert result.scalar_one_or_none() is None


# =====================================================================
# 5. unsubscribe_returns_204 — happy path
# =====================================================================

@pytest.mark.asyncio
async def test_unsubscribe_returns_204(
    client: TestClient, db_session
):
    """Unsubscribing from a subscribed SKU returns 204."""
    user_id = "sub-user-005"
    sku_id = "sku-sub-005"
    mock_data = _mock_sku_ok(sku_id=sku_id, price=150.0)

    b2b_client.get_sku_by_id = AsyncMock(return_value=mock_data)

    # First subscribe
    client.post(
        "/api/v1/cart/subscribe",
        json={"sku_id": sku_id, "notify_on": "in_stock"},
        headers={"X-User-Id": user_id},
    )

    # Verify subscription exists
    result = await db_session.execute(
        select(SubscriptionModel).where(
            (SubscriptionModel.user_id == user_id) &
            (SubscriptionModel.sku_id == sku_id)
        )
    )
    assert result.scalar_one_or_none() is not None

    # Now unsubscribe
    response = client.delete(
        f"/api/v1/cart/subscribe/{sku_id}",
        headers={"X-User-Id": user_id},
    )
    assert response.status_code == 204

    # Verify removed from DB
    result = await db_session.execute(
        select(SubscriptionModel).where(
            (SubscriptionModel.user_id == user_id) &
            (SubscriptionModel.sku_id == sku_id)
        )
    )
    assert result.scalar_one_or_none() is None


# =====================================================================
# 6. unsubscribe_idempotent — already absent → 204
# =====================================================================

@pytest.mark.asyncio
async def test_unsubscribe_idempotent(
    client: TestClient, db_session
):
    """Unsubscribing from a non-subscribed SKU returns 204 (idempotent)."""
    user_id = "sub-user-006"
    sku_id = "sku-sub-006"

    # Never subscribed
    response = client.delete(
        f"/api/v1/cart/subscribe/{sku_id}",
        headers={"X-User-Id": user_id},
    )
    assert response.status_code == 204


# =====================================================================
# 7. IDOR protection — no query param user_id
# =====================================================================

@pytest.mark.asyncio
async def test_subscription_idor_protection(
    client: TestClient, db_session
):
    """user_id must come from X-User-Id header, not query/body."""
    user_a = "sub-user-a"
    user_b = "sub-user-b"
    sku_id = "sku-sub-007"
    mock_data = _mock_sku_ok(sku_id=sku_id, price=400.0)

    b2b_client.get_sku_by_id = AsyncMock(return_value=mock_data)

    # User A subscribes
    client.post(
        "/api/v1/cart/subscribe",
        json={"sku_id": sku_id, "notify_on": "in_stock"},
        headers={"X-User-Id": user_a},
    )

    # User B tries to subscribe to the same SKU — should create a NEW subscription
    # (not interfere with A's)
    response = client.post(
        "/api/v1/cart/subscribe",
        json={"sku_id": sku_id, "notify_on": "in_stock"},
        headers={"X-User-Id": user_b},
    )
    assert response.status_code == 201

    # Verify both exist independently
    result_a = await db_session.execute(
        select(SubscriptionModel).where(
            (SubscriptionModel.user_id == user_a) &
            (SubscriptionModel.sku_id == sku_id)
        )
    )
    result_b = await db_session.execute(
        select(SubscriptionModel).where(
            (SubscriptionModel.user_id == user_b) &
            (SubscriptionModel.sku_id == sku_id)
        )
    )
    assert result_a.scalar_one_or_none() is not None
    assert result_b.scalar_one_or_none() is not None


# =====================================================================
# 8. missing X-User-Id header → 401
# =====================================================================

@pytest.mark.asyncio
async def test_missing_x_user_id_returns_401(
    client: TestClient, db_session
):
    """Without X-User-Id header → 401 Unauthorized."""
    sku_id = "sku-sub-008"
    mock_data = _mock_sku_ok(sku_id=sku_id, price=600.0)

    b2b_client.get_sku_by_id = AsyncMock(return_value=mock_data)

    response = client.post(
        "/api/v1/cart/subscribe",
        json={"sku_id": sku_id, "notify_on": "in_stock"},
    )
    assert response.status_code == 401
