"""Tests for recommendations endpoints."""

from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from src.services.b2b_client import b2b_client


def test_get_recommendations(client: TestClient):
    """Test getting product recommendations with mocked B2B."""
    b2b_client.get_product_by_id = AsyncMock(return_value={
        "id": "test_product_1",
        "category_id": "cat1",
        "parent_category_id": "cat_parent",
    })
    b2b_client.get_products_by_category = AsyncMock(return_value={
        "items": [
            {"id": "rec1", "title": "Rec 1", "cover_image": "http://img", "min_price": 100.0},
            {"id": "rec2", "title": "Rec 2", "cover_image": "http://img", "min_price": 200.0},
        ],
        "total_count": 2,
        "limit": 8,
        "offset": 0,
    })

    response = client.get("/api/v1/recommendations/products/test_product_1?limit=4")
    assert response.status_code == 200
    data = response.json()
    assert "current_product_id" in data
    assert "recommendations" in data
    assert "reason" in data
    assert len(data["recommendations"]) == 2


def test_get_recommendations_product_not_found(client: TestClient):
    """Test recommendations when product not found."""
    b2b_client.get_product_by_id = AsyncMock(return_value=None)

    response = client.get("/api/v1/recommendations/products/nonexistent?limit=4")
    assert response.status_code == 200
    data = response.json()
    assert data["current_product_id"] == "nonexistent"
    assert data["recommendations"] == []
    assert data["reason"] == "Product not found"
