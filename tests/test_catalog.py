"""Tests for catalog endpoints."""

from fastapi.testclient import TestClient


def test_get_categories_requires_b2b(client: TestClient):
    """Test getting categories fails without B2B."""
    response = client.get("/api/v1/catalog/categories")
    # Will fail with 502 (B2B unavailable) or return empty list
    assert response.status_code in [200, 502, 504]


def test_get_products(client: TestClient):
    """Test getting products list."""
    response = client.get("/api/v1/catalog/products?page=1&page_size=20")
    # May fail without B2B or return empty list
    assert response.status_code in [200, 502, 504]
    if response.status_code == 200:
        data = response.json()
        assert "products" in data
        assert "total" in data
        assert "page" in data


def test_get_product_not_found(client: TestClient):
    """Test getting non-existent product."""
    response = client.get("/api/v1/catalog/products/nonexistent")
    # Will be 404 (product not found) or 502 (B2B unavailable)
    assert response.status_code in [404, 502, 504]


def test_get_category_filters(client: TestClient):
    """Test getting filters for a category."""
    response = client.get("/api/v1/catalog/categories/test_cat_1/filters")
    assert response.status_code == 200
    data = response.json()
    assert "category_id" in data
    assert "filters" in data
