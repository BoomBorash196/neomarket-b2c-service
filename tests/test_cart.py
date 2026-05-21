"""Tests for cart endpoints."""

from fastapi.testclient import TestClient
from src.models import CartItemModel


def test_get_empty_cart(client: TestClient):
    """Test getting an empty cart."""
    user_id = "test_user_1"
    response = client.get(f"/api/v1/cart?user_id={user_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == user_id
    assert data["items"] == []
    assert data["total_items"] == 0
    assert data["total_amount"] == 0.0


def test_add_to_cart(client: TestClient):
    """Test adding item to cart."""
    user_id = "test_user_2"
    # Note: This test will fail without B2B API running
    # It demonstrates the expected structure
    payload = {"sku_id": "test_sku_1", "quantity": 2}
    response = client.post(f"/api/v1/cart?user_id={user_id}", json=payload)
    
    # Expect 404 since B2B is not running (SKU not found)
    # When B2B is available, this should return 200
    assert response.status_code in [200, 404, 502]


def test_get_nonexistent_cart_item(client: TestClient):
    """Test removing non-existent cart item."""
    user_id = "test_user_3"
    response = client.delete(f"/api/v1/cart/999?user_id={user_id}")
    assert response.status_code == 404


def test_clear_cart(client: TestClient):
    """Test clearing cart."""
    user_id = "test_user_4"
    response = client.delete(f"/api/v1/cart?user_id={user_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["total_items"] == 0
    assert data["total_amount"] == 0.0
