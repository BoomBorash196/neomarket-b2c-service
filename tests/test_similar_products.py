"""Tests for GET /api/v1/catalog/products/{product_id}/similar — US-CAT-04.

Covers:
  - similar_returns_up_to_8_from_same_category     (happy path, current product excluded)
  - empty_category_returns_200_empty_list           (no similar → 200 with [])
  - unknown_product_returns_404                     (non-existent product → 404)
  - fallback_to_parent_category                     (not enough in same category → parent)
  - current_product_excluded_from_results           (current product never in similar)
  - b2b_unavailable_returns_502                     (B2B down → 502)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app

CATALOG_B2B = "src.routes.catalog.b2b_client"


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def client():
    """Test client with B2B mocked."""
    with TestClient(app=app, raise_server_exceptions=False) as c:
        yield c


# ======================================================================
# Helpers
# ======================================================================

def _make_product_detail(
    product_id: str,
    title: str,
    price: float,
    category_id: str = "cat1",
    parent_category_id: str | None = None,
    active_quantity: int = 1,
):
    return {
        "id": product_id,
        "title": title,
        "main_image_url": "http://img",
        "min_price": price,
        "active_quantity": active_quantity,
        "description": "",
        "images": [],
        "characteristics": {},
        "skus": [],
        "category_id": category_id,
        "parent_category_id": parent_category_id,
    }


def _make_similar_product(product_id: str, title: str, price: float = 100.0):
    return {
        "id": product_id,
        "title": title,
        "main_image_url": "http://img",
        "min_price": price,
        "active_quantity": 1,
    }


def _mock_similar_result(products):
    return {"items": products}


# ======================================================================
# TEST 1 — similar_returns_up_to_8_from_same_category
# ======================================================================

def test_similar_returns_up_to_8_from_same_category(client: TestClient):
    """Happy path: up to 8 similar products from same category, current excluded."""
    current = _make_product_detail("p-current", "Current Product", 500.0, category_id="cat1")

    similar_products = [
        _make_similar_product(f"p-{i}", f"Similar {i}", 100.0 + i * 10)
        for i in range(1, 11)  # 10 products available
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=current)
        mock_b2b.get_similar_products = AsyncMock(
            return_value=_mock_similar_result(similar_products)
        )

        resp = client.get("/api/v1/catalog/products/p-current/similar")

    assert resp.status_code == 200
    data = resp.json()

    assert data["current_product_id"] == "p-current"
    assert len(data["recommendations"]) == 8  # capped at 8
    assert data["reason"] == "same_category"

    # Current product must NOT be in results
    rec_ids = [r["id"] for r in data["recommendations"]]
    assert "p-current" not in rec_ids

    # Verify B2B was called with correct params
    call_kwargs = mock_b2b.get_similar_products.call_args.kwargs
    assert call_kwargs["product_id"] == "p-current"
    assert call_kwargs["category_id"] == "cat1"
    assert call_kwargs["limit"] == 8


def test_similar_returns_fewer_than_8(client: TestClient):
    """Happy path: fewer than 8 available → returns all available."""
    current = _make_product_detail("p-1", "Product", 100.0, category_id="cat1")
    similar_products = [
        _make_similar_product("p-2", "Similar A"),
        _make_similar_product("p-3", "Similar B"),
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=current)
        mock_b2b.get_similar_products = AsyncMock(
            return_value=_mock_similar_result(similar_products)
        )

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["recommendations"]) == 2
    assert data["reason"] == "same_category"  # came from same category


# ======================================================================
# TEST 2 — empty_category_returns_200_empty_list
# ======================================================================

def test_empty_category_returns_200_empty_list(client: TestClient):
    """No similar products → 200 with empty recommendations list."""
    current = _make_product_detail("p-1", "Standalone Product", 100.0, category_id="cat-empty")

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=current)
        mock_b2b.get_similar_products = AsyncMock(
            return_value=_mock_similar_result([])
        )

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    assert data["current_product_id"] == "p-1"
    assert data["recommendations"] == []
    assert len(data["recommendations"]) == 0


def test_empty_category_no_parent_returns_200_empty_list(client: TestClient):
    """No similar + no parent category → 200 with empty list."""
    current = _make_product_detail(
        "p-1", "Isolated Product", 100.0,
        category_id="cat-iso",
        parent_category_id=None,
    )

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=current)
        mock_b2b.get_similar_products = AsyncMock(
            return_value=_mock_similar_result([])
        )

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    assert data["recommendations"] == []
    assert data["reason"] == "no_similar_products"


# ======================================================================
# TEST 3 — unknown_product_returns_404
# ======================================================================

def test_unknown_product_returns_404(client: TestClient):
    """Non-existent product → 404 PRODUCT_NOT_FOUND."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=None)

        resp = client.get("/api/v1/catalog/products/nonexistent/similar")

    assert resp.status_code == 404
    data = resp.json()
    assert data["code"] == "PRODUCT_NOT_FOUND"
    assert "not found" in data["message"].lower()


# ======================================================================
# TEST 4 — fallback_to_parent_category
# ======================================================================

def test_fallback_to_parent_category(client: TestClient):
    """Not enough in same category → fill from parent category."""
    current = _make_product_detail(
        "p-1", "Product", 100.0,
        category_id="cat-children",
        parent_category_id="cat-parent",
    )

    # Only 2 similar in same category
    same_cat_similar = [
        _make_similar_product("p-sim-1", "Same Cat 1"),
        _make_similar_product("p-sim-2", "Same Cat 2"),
    ]

    # Parent category has more
    parent_cat_similar = [
        _make_similar_product(f"p-parent-{i}", f"Parent Cat {i}")
        for i in range(1, 8)
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=current)
        mock_b2b.get_similar_products = AsyncMock(
            side_effect=[
                _mock_similar_result(same_cat_similar),       # first call: same category
                _mock_similar_result(parent_cat_similar),      # second call: parent category
            ]
        )

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()

    # Should have 2 from same + 6 from parent = 8
    assert len(data["recommendations"]) == 8

    rec_ids = [r["id"] for r in data["recommendations"]]
    assert "p-sim-1" in rec_ids
    assert "p-sim-2" in rec_ids
    assert "p-parent-1" in rec_ids
    assert "p-parent-6" in rec_ids

    # Should have been called twice
    assert mock_b2b.get_similar_products.call_count == 2


def test_no_fallback_when_no_parent_category(client: TestClient):
    """When product has no parent_category_id, no fallback is attempted."""
    current = _make_product_detail(
        "p-1", "Product", 100.0,
        category_id="cat1",
        parent_category_id=None,
    )

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=current)
        mock_b2b.get_similar_products = AsyncMock(
            return_value=_mock_similar_result([])
        )

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    assert data["recommendations"] == []

    # Only called once (no parent fallback)
    assert mock_b2b.get_similar_products.call_count == 1


# ======================================================================
# TEST 5 — current_product_excluded_from_results
# ======================================================================

def test_current_product_excluded_from_results(client: TestClient):
    """Current product must never appear in similar results, even if B2B returns it."""
    current = _make_product_detail("p-1", "Product", 100.0, category_id="cat1")

    similar_products = [
        _make_similar_product("p-1", "Current Product"),  # B2B mistakenly includes current
        _make_similar_product("p-2", "Similar A"),
        _make_similar_product("p-3", "Similar B"),
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=current)
        mock_b2b.get_similar_products = AsyncMock(
            return_value=_mock_similar_result(similar_products)
        )

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    rec_ids = [r["id"] for r in data["recommendations"]]
    assert "p-1" not in rec_ids
    assert len(data["recommendations"]) == 2  # only non-current ones


def test_current_product_excluded_from_parent_fallback(client: TestClient):
    """Current product must also be excluded during parent category fallback."""
    current = _make_product_detail(
        "p-1", "Product", 100.0,
        category_id="cat-child",
        parent_category_id="cat-parent",
    )

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=current)
        mock_b2b.get_similar_products = AsyncMock(
            side_effect=[
                _mock_similar_result([]),  # no results from same category
                _mock_similar_result([     # parent includes current by mistake
                    _make_similar_product("p-1", "Current Product"),
                    _make_similar_product("p-parent-1", "Parent 1"),
                    _make_similar_product("p-parent-2", "Parent 2"),
                ]),
            ]
        )

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    rec_ids = [r["id"] for r in data["recommendations"]]
    assert "p-1" not in rec_ids
    assert len(data["recommendations"]) == 2  # only parent ones, current excluded


# ======================================================================
# TEST 6 — duplicate_product_ids_filtered
# ======================================================================

def test_duplicate_product_ids_filtered(client: TestClient):
    """If same product appears in both same-category and parent-category results, it's deduplicated."""
    current = _make_product_detail(
        "p-1", "Product", 100.0,
        category_id="cat-child",
        parent_category_id="cat-parent",
    )

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=current)
        mock_b2b.get_similar_products = AsyncMock(
            side_effect=[
                _mock_similar_result([
                    _make_similar_product("p-same-1", "Same 1"),
                    _make_similar_product("p-dup", "Dup in both"),
                ]),
                _mock_similar_result([
                    _make_similar_product("p-dup", "Dup in both"),  # duplicate
                    _make_similar_product("p-parent-1", "Parent 1"),
                ]),
            ]
        )

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    rec_ids = [r["id"] for r in data["recommendations"]]
    assert rec_ids.count("p-dup") == 1  # deduplicated
    assert len(data["recommendations"]) == 3


# ======================================================================
# TEST 7 — b2b_unavailable_returns_502
# ======================================================================

def _b2b_error_mock(message: str = "Service Unavailable"):
    """Create an async mock that raises B2BClientError."""
    from src.services.b2b_client import B2BClientError

    async def _raise(*args, **kwargs):
        raise B2BClientError(status_code=503, message=message)

    return _raise


def test_b2b_unavailable_product_lookup_returns_502(client: TestClient):
    """When B2B is unavailable during product lookup → 502."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = _b2b_error_mock()

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_similar_lookup_returns_502(client: TestClient):
    """When B2B is unavailable during similar lookup → 502."""
    current = _make_product_detail("p-1", "Product", 100.0, category_id="cat1")

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=current)
        mock_b2b.get_similar_products = _b2b_error_mock()

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"


# ======================================================================
# TEST 8 — response schema correctness
# ======================================================================

def test_similar_response_schema(client: TestClient):
    """Response matches RecommendationList schema."""
    current = _make_product_detail("p-1", "Product", 100.0, category_id="cat1")
    similar_products = [
        _make_similar_product("p-2", "Wireless Headphones", 4999.0),
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=current)
        mock_b2b.get_similar_products = AsyncMock(
            return_value=_mock_similar_result(similar_products)
        )

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()

    assert "current_product_id" in data
    assert "recommendations" in data
    assert "reason" in data

    assert data["current_product_id"] == "p-1"
    assert isinstance(data["recommendations"], list)
    assert isinstance(data["reason"], str)

    rec = data["recommendations"][0]
    assert "id" in rec
    assert "name" in rec
    assert "main_image_url" in rec
    assert "min_price" in rec
    assert "has_stock" in rec

    assert rec["id"] == "p-2"
    assert rec["name"] == "Wireless Headphones"
    assert rec["min_price"] == 4999.0
