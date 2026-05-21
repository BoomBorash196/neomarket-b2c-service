"""Tests for wishlist endpoints."""

from fastapi.testclient import TestClient


def test_get_empty_wishlist(client: TestClient):
    """Test getting empty wishlist."""
    user_id = "test_user_8"
    response = client.get(f"/api/v1/wishlist?user_id={user_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == user_id
    assert data["items"] == []


def test_add_to_wishlist_requires_b2b(client: TestClient):
    """Test adding to wishlist fails without B2B."""
    user_id = "test_user_9"
    payload = {"product_id": "test_product_1"}
    response = client.post("/api/v1/wishlist", json=payload, params={"user_id": user_id})
    # Will fail with 404 (product not found in B2B) or 502
    assert response.status_code in [404, 502, 504]


def test_remove_nonexistent_from_wishlist(client: TestClient):
    """Test removing non-existent wishlist item."""
    user_id = "test_user_10"
    response = client.delete(f"/api/v1/wishlist/nonexistent?user_id={user_id}")
    assert response.status_code == 404
