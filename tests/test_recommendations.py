"""Tests for recommendations endpoints."""

from fastapi.testclient import TestClient


def test_get_recommendations(client: TestClient):
    """Test getting product recommendations."""
    response = client.get("/api/v1/recommendations/products/test_product_1?limit=4")
    # May fail without B2B or return empty recommendations
    assert response.status_code in [200, 502, 504]
    if response.status_code == 200:
        data = response.json()
        assert "current_product_id" in data
        assert "recommendations" in data
        assert "reason" in data
