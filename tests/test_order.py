"""Tests for order endpoints."""

from fastapi.testclient import TestClient
from src.models import OrderModel
from src.schemas import OrderStatus


def test_get_empty_orders(client: TestClient):
    """Test getting empty order list."""
    user_id = "test_user_5"
    response = client.get(f"/api/v1/orders?user_id={user_id}")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


def test_create_order_requires_b2b(client: TestClient):
    """Test creating order fails without B2B."""
    user_id = "test_user_6"
    payload = {
        "user_id": user_id,
        "items": [
            {"sku_id": "test_sku_1", "quantity": 1, "price_at_order": 100.0}
        ],
        "total_amount": 100.0
    }
    response = client.post("/api/v1/orders", json=payload)
    # Will fail with 502 (B2B unavailable) or 404
    assert response.status_code in [404, 502, 504]


def test_get_nonexistent_order(client: TestClient):
    """Test getting non-existent order."""
    user_id = "test_user_7"
    response = client.get(f"/api/v1/orders/999?user_id={user_id}")
    assert response.status_code == 404
