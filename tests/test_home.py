"""Tests for home page endpoints."""

from fastapi.testclient import TestClient


def test_get_home_data(client: TestClient):
    """Test getting home page data."""
    response = client.get("/api/v1/home")
    assert response.status_code == 200
    data = response.json()
    assert "banners" in data
    assert "collections" in data
    assert "featured_categories" in data


def test_get_active_banners(client: TestClient):
    """Test getting active banners."""
    response = client.get("/api/v1/home/banners")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


def test_get_collections(client: TestClient):
    """Test getting collections."""
    response = client.get("/api/v1/home/collections")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
