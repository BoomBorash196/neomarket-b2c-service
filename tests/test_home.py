"""Tests for home page endpoints."""

import pytest
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


@pytest.mark.asyncio
async def test_banner_response_contains_openapi_required_fields(
    client: TestClient, db_session
):
    """OpenAPI Banner requires id, image_url and link."""
    from src.models import BannerModel

    db_session.add(
        BannerModel(
            title="Sale",
            image_url="https://cdn.example/banner.png",
            link_url="https://example/sale",
            priority=10,
            is_active=True,
        )
    )
    await db_session.commit()

    response = client.get("/api/v1/home/banners")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    banner = data[0]
    for field in ("id", "image_url", "link"):
        assert field in banner, f"missing required field {field}"
    assert banner["id"] == banner["banner_id"]
    assert banner["link"] == banner["link_url"]


@pytest.mark.asyncio
async def test_collection_response_contains_openapi_required_fields(
    client: TestClient, db_session
):
    """OpenAPI Collection requires id, name and products."""
    from src.models import CollectionModel

    db_session.add(
        CollectionModel(
            title="Хиты",
            description="",
            product_ids="[]",
            max_display=8,
            is_active=True,
        )
    )
    await db_session.commit()

    response = client.get("/api/v1/home/collections")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    collection = data[0]
    for field in ("id", "name", "products"):
        assert field in collection, f"missing required field {field}"
    assert collection["id"] == collection["collection_id"]
    assert collection["name"] == collection["title"]
