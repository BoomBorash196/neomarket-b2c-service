"""Tests for collection endpoints — collections list + single collection with B2B enrichment."""

import json
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient
from src.models import CollectionModel
from src.services.b2b_client import b2b_client


# =====================================================================
# 1. collections_list_returns_metadata_without_products
# =====================================================================

@pytest.mark.asyncio
async def test_collections_list_returns_metadata_without_products(
    client: TestClient, db_session
):
    """GET /collections returns only metadata — no product details, no unavailable_ids."""
    db_session.add(CollectionModel(
        title="Хиты продаж",
        description="Самые популярные товары",
        product_ids=json.dumps(["prod-1", "prod-2"]),
        max_display=8,
        is_active=True,
    ))
    db_session.add(CollectionModel(
        title="Новинки сезона",
        description="Свежие поступления",
        product_ids=json.dumps(["prod-3"]),
        max_display=4,
        is_active=True,
    ))
    await db_session.commit()

    response = client.get("/api/v1/home/collections")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2

    # Check metadata fields are present
    titles = {c["title"] for c in data}
    assert "Хиты продаж" in titles
    assert "Новинки сезона" in titles

    # Each collection has empty products and empty unavailable_ids
    for c in data:
        assert c["products"] == []
        assert c["unavailable_ids"] == []
        assert "collection_id" in c
        assert "description" in c
        assert "max_display" in c


# =====================================================================
# 2. collection_products_enriched_from_b2b
# =====================================================================

@pytest.mark.asyncio
async def test_collection_products_enriched_from_b2b(client: TestClient, db_session):
    """GET /collections/{id} returns products enriched from B2B batch."""
    collection = CollectionModel(
        title="Хиты продаж",
        description="Самые популярные товары",
        product_ids=json.dumps(["prod-1", "prod-2"]),
        max_display=8,
        is_active=True,
    )
    db_session.add(collection)
    await db_session.commit()

    # Mock B2B batch response
    b2b_response = {
        "prod-1": {
            "title": "Product One",
            "main_image_url": "https://example.com/p1.jpg",
            "min_price": 1000.0,
            "is_active": True,
            "is_available": True,
        },
        "prod-2": {
            "title": "Product Two",
            "main_image_url": "https://example.com/p2.jpg",
            "min_price": 2000.0,
            "is_active": True,
            "is_available": True,
        },
    }

    with patch.object(b2b_client, 'get_products_by_ids', new_callable=AsyncMock) as mock_batch:
        mock_batch.return_value = b2b_response
        response = client.get(f"/api/v1/home/collections/{collection.collection_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Хиты продаж"
    assert data["description"] == "Самые популярные товары"
    assert len(data["products"]) == 2

    product_ids = {p["product_id"] for p in data["products"]}
    assert "prod-1" in product_ids
    assert "prod-2" in product_ids

    # Check product fields
    for p in data["products"]:
        assert "product_id" in p
        assert "title" in p
        assert "main_image_url" in p
        assert "min_price" in p

    assert data["unavailable_ids"] == []


# =====================================================================
# 3. unavailable_products_in_unavailable_ids
# =====================================================================

@pytest.mark.asyncio
async def test_unavailable_products_in_unavailable_ids(client: TestClient, db_session):
    """Products not found / inactive in B2B → unavailable_ids, excluded from items."""
    collection = CollectionModel(
        title="Хиты продаж",
        product_ids=json.dumps(["prod-available", "prod-deleted", "prod-inactive"]),
        max_display=8,
        is_active=True,
    )
    db_session.add(collection)
    await db_session.commit()

    # B2B returns only the available product; deleted and inactive are missing
    b2b_response = {
        "prod-available": {
            "title": "Available Product",
            "main_image_url": "https://example.com/avail.jpg",
            "min_price": 500.0,
            "is_active": True,
            "is_available": True,
        },
        # "prod-deleted" is missing → not found in B2B
        "prod-inactive": {
            "title": "Inactive Product",
            "main_image_url": "https://example.com/inact.jpg",
            "min_price": 300.0,
            "is_active": False,  # explicitly inactive
            "is_available": True,
        },
    }

    with patch.object(b2b_client, 'get_products_by_ids', new_callable=AsyncMock) as mock_batch:
        mock_batch.return_value = b2b_response
        response = client.get(f"/api/v1/home/collections/{collection.collection_id}")

    assert response.status_code == 200
    data = response.json()

    # Only the available product is in items
    assert len(data["products"]) == 1
    assert data["products"][0]["product_id"] == "prod-available"

    # Deleted and inactive are in unavailable_ids
    assert sorted(data["unavailable_ids"]) == sorted(["prod-deleted", "prod-inactive"])


# =====================================================================
# 4. unknown_collection_returns_404
# =====================================================================

def test_unknown_collection_returns_404(client: TestClient, db_session):
    """GET /collections/{id} for a non-existent collection → 404."""
    response = client.get("/api/v1/home/collections/99999")

    assert response.status_code == 404
    data = response.json()
    assert data["code"] == "COLLECTION_NOT_FOUND"


# =====================================================================
# 5. all_products_unavailable_returns_empty_items
# =====================================================================

@pytest.mark.asyncio
async def test_all_products_unavailable_returns_empty_items(client: TestClient, db_session):
    """When all products are unavailable → items=[], unavailable_ids=[...]. Not an error."""
    collection = CollectionModel(
        title="Снято с продажи",
        product_ids=json.dumps(["prod-1", "prod-2"]),
        max_display=8,
        is_active=True,
    )
    db_session.add(collection)
    await db_session.commit()

    # B2B returns nothing — all products are gone
    with patch.object(b2b_client, 'get_products_by_ids', new_callable=AsyncMock) as mock_batch:
        mock_batch.return_value = {}
        response = client.get(f"/api/v1/home/collections/{collection.collection_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["products"] == []
    assert sorted(data["unavailable_ids"]) == ["prod-1", "prod-2"]


# =====================================================================
# 6. max_display_limits_enriched_items
# =====================================================================

@pytest.mark.asyncio
async def test_max_display_limits_enriched_items(client: TestClient, db_session):
    """Only up to max_display products are returned in items."""
    collection = CollectionModel(
        title="Большая подборка",
        product_ids=json.dumps(["prod-1", "prod-2", "prod-3", "prod-4", "prod-5"]),
        max_display=3,
        is_active=True,
    )
    db_session.add(collection)
    await db_session.commit()

    b2b_response = {}
    for i in range(1, 6):
        b2b_response[f"prod-{i}"] = {
            "title": f"Product {i}",
            "main_image_url": f"https://example.com/p{i}.jpg",
            "min_price": float(i * 100),
            "is_active": True,
            "is_available": True,
        }

    with patch.object(b2b_client, 'get_products_by_ids', new_callable=AsyncMock) as mock_batch:
        mock_batch.return_value = b2b_response
        response = client.get(f"/api/v1/home/collections/{collection.collection_id}")

    assert response.status_code == 200
    data = response.json()
    assert len(data["products"]) == 3  # limited by max_display
    # The remaining 2 are NOT in unavailable_ids because they weren't processed past the limit
    assert data["unavailable_ids"] == []