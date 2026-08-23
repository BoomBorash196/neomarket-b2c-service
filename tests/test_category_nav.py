"""Tests for category navigation endpoints — US-CAT-05.

Covers:
  - category_tree_returns_nested_structure              (tree from flat list)
  - category_detail_returns_single_category             (single category lookup)
  - category_not_found_returns_404                      (unknown category → 404)
  - breadcrumbs_return_path_from_root                   (chain from root to category)
  - breadcrumbs_from_product_id                         (resolved via product)
  - ambiguous_params_returns_400                        (both category_id & product_id → 400)
  - missing_params_returns_400                          (neither param → 400)
  - orphan_node_returns_422                             (broken hierarchy → 422)
  - cycle_detected_returns_422                          (cycle in hierarchy → 422)
  - b2b_unavailable_returns_502                         (B2B down → 502)
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
# Helpers — flat category data
# ======================================================================

def _flat_categories():
    """Return a flat list of categories forming a tree:

    Electronics (cat-root)
    ├── Phones (cat-phones, parent: cat-root)
    │   ├── iPhone (cat-iphone, parent: cat-phones)
    │   └── Samsung (cat-samsung, parent: cat-phones)
    └── Laptops (cat-laptops, parent: cat-root)
    """
    return [
        {"category_id": "cat-root", "name": "Electronics", "parent_id": None},
        {"category_id": "cat-phones", "name": "Phones", "parent_id": "cat-root"},
        {"category_id": "cat-iphone", "name": "iPhone", "parent_id": "cat-phones"},
        {"category_id": "cat-samsung", "name": "Samsung", "parent_id": "cat-phones"},
        {"category_id": "cat-laptops", "name": "Laptops", "parent_id": "cat-root"},
    ]


def _b2b_error_mock(message: str = "Service Unavailable"):
    """Create an async mock that raises B2BClientError."""
    from src.services.b2b_client import B2BClientError

    async def _raise(*args, **kwargs):
        raise B2BClientError(status_code=503, message=message)

    return _raise


# ======================================================================
# TEST 1 — category_tree_returns_nested_structure
# ======================================================================

def test_category_tree_returns_nested_structure(client: TestClient):
    """Tree is built from a flat B2B list into nested children."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/categories/tree")

    assert resp.status_code == 200
    data = resp.json()

    # Top-level: only Electronics (cat-root) — Laptops is child of cat-root
    assert len(data) == 1

    electronics = data[0]
    assert electronics["category_id"] == "cat-root"
    assert electronics["name"] == "Electronics"
    assert electronics["parent_id"] is None
    assert len(electronics["children"]) == 2  # Phones, Laptops

    phones = next(c for c in electronics["children"] if c["category_id"] == "cat-phones")
    assert phones["name"] == "Phones"
    assert phones["parent_id"] == "cat-root"
    assert len(phones["children"]) == 2  # iPhone, Samsung

    iphone = next(c for c in phones["children"] if c["category_id"] == "cat-iphone")
    assert iphone["name"] == "iPhone"
    assert len(iphone["children"]) == 0

    laptops = next(c for c in electronics["children"] if c["category_id"] == "cat-laptops")
    assert laptops["name"] == "Laptops"
    assert len(laptops["children"]) == 0


def test_category_tree_empty(client: TestClient):
    """Empty B2B response → empty list."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=[])

        resp = client.get("/api/v1/catalog/categories/tree")

    assert resp.status_code == 200
    data = resp.json()
    assert data == []


# ======================================================================
# TEST 2 — category_detail_returns_single_category
# ======================================================================

def test_category_detail_returns_single_category(client: TestClient):
    """GET /categories/{id} returns correct category detail."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/categories/cat-iphone")

    assert resp.status_code == 200
    data = resp.json()
    assert data["category_id"] == "cat-iphone"
    assert data["name"] == "iPhone"
    assert data["parent_id"] == "cat-phones"


def test_category_detail_root(client: TestClient):
    """Root category detail returns parent_id = null."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/categories/cat-root")

    assert resp.status_code == 200
    data = resp.json()
    assert data["category_id"] == "cat-root"
    assert data["parent_id"] is None


# ======================================================================
# TEST 3 — unknown_category_returns_404
# ======================================================================

def test_unknown_category_returns_404(client: TestClient):
    """Non-existent category → 404 CATEGORY_NOT_FOUND."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/categories/nonexistent")

    assert resp.status_code == 404
    data = resp.json()
    assert data["code"] == "CATEGORY_NOT_FOUND"


# ======================================================================
# TEST 4 — breadcrumbs_return_path_from_root
# ======================================================================

def test_breadcrumbs_return_path_from_root(client: TestClient):
    """Breadcrumbs for cat-iphone: Electronics → Phones → iPhone."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-iphone"})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 3

    assert data["items"][0]["category_id"] == "cat-root"
    assert data["items"][0]["name"] == "Electronics"

    assert data["items"][1]["category_id"] == "cat-phones"
    assert data["items"][1]["name"] == "Phones"

    assert data["items"][2]["category_id"] == "cat-iphone"
    assert data["items"][2]["name"] == "iPhone"


def test_breadcrumbs_root_category(client: TestClient):
    """Breadcrumbs for root category is a single item."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-root"})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["category_id"] == "cat-root"


def test_breadcrumbs_level_2_category(client: TestClient):
    """Breadcrumbs for cat-phones: Electronics → Phones."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-phones"})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["items"][0]["category_id"] == "cat-root"
    assert data["items"][1]["category_id"] == "cat-phones"


# ======================================================================
# TEST 5 — breadcrumbs_from_product_id
# ======================================================================

def test_breadcrumbs_from_product_id(client: TestClient):
    """When product_id given, category resolved from product, then breadcrumbs built."""
    product = {
        "product_id": "p1",
        "title": "iPhone 15",
        "category_id": "cat-iphone",
        "parent_category_id": "cat-phones",
        "main_image_url": "http://img",
        "min_price": 999.0,
        "is_available": True,
        "description": "",
        "images": [],
        "characteristics": {},
        "skus": [],
    }

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=product)
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"product_id": "p1"})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 3
    assert data["items"][2]["category_id"] == "cat-iphone"


def test_breadcrumbs_product_not_found_returns_404(client: TestClient):
    """Product not found → 404."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=None)

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"product_id": "ghost"})

    assert resp.status_code == 404
    data = resp.json()
    assert data["code"] == "PRODUCT_NOT_FOUND"


# ======================================================================
# TEST 6 — ambiguous_params_returns_400
# ======================================================================

def test_ambiguous_params_returns_400(client: TestClient):
    """Both category_id and product_id → 400 AMBIGUOUS_PARAMS."""
    resp = client.get(
        "/api/v1/catalog/breadcrumbs",
        params={"category_id": "cat-iphone", "product_id": "p1"},
    )

    assert resp.status_code == 400
    data = resp.json()
    assert data["code"] == "AMBIGUOUS_PARAMS"
    assert "category_id" in data["message"]
    assert "product_id" in data["message"]


def test_missing_params_returns_400(client: TestClient):
    """Neither category_id nor product_id → 400 MISSING_PARAMS."""
    resp = client.get("/api/v1/catalog/breadcrumbs")

    assert resp.status_code == 400
    data = resp.json()
    assert data["code"] == "MISSING_PARAMS"
    assert "category_id" in data["message"] or "product_id" in data["message"]


# ======================================================================
# TEST 7 — orphan_node_returns_422
# ======================================================================

def test_orphan_node_returns_422(client: TestClient):
    """Category with parent_id pointing to non-existent category → 422."""
    broken = [
        {"category_id": "cat-orphan", "name": "Orphan", "parent_id": "cat-missing"},
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=broken)

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-orphan"})

    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "ORPHAN_NODE"


def test_orphan_in_middle_returns_422(client: TestClient):
    """Broken hierarchy in the middle of the chain → 422."""
    broken = [
        {"category_id": "cat-a", "name": "A", "parent_id": "cat-b"},
        {"category_id": "cat-b", "name": "B", "parent_id": "cat-missing"},
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=broken)

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-a"})

    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "ORPHAN_NODE"


# ======================================================================
# TEST 8 — cycle_detected_returns_422
# ======================================================================

def test_cycle_detected_returns_422(client: TestClient):
    """Circular parent reference → 422."""
    cycle = [
        {"category_id": "cat-x", "name": "X", "parent_id": "cat-y"},
        {"category_id": "cat-y", "name": "Y", "parent_id": "cat-x"},
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=cycle)

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-x"})

    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "ORPHAN_NODE"
    assert "Cycle" in data["message"] or "cycle" in data["message"]


# ======================================================================
# TEST 9 — b2b_unavailable_returns_502
# ======================================================================

def test_b2b_unavailable_tree(client: TestClient):
    """B2B down during tree fetch → 502."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = _b2b_error_mock()

        resp = client.get("/api/v1/catalog/categories/tree")

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_category_detail(client: TestClient):
    """B2B down during category detail → 502."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = _b2b_error_mock()

        resp = client.get("/api/v1/catalog/categories/cat-iphone")

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_breadcrumbs(client: TestClient):
    """B2B down during breadcrumbs → 502."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = _b2b_error_mock()

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-iphone"})

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"


# ======================================================================
# TEST 10 — breadcrumbs for unknown category → 404
# ======================================================================

def test_breadcrumbs_unknown_category_returns_404(client: TestClient):
    """Breadcrumbs for non-existent category → 404."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "ghost-cat"})

    assert resp.status_code == 404
    data = resp.json()
    assert data["code"] == "CATEGORY_NOT_FOUND"


# ======================================================================
# TEST 11 — response schema correctness
# ======================================================================

def test_breadcrumbs_response_schema(client: TestClient):
    """Breadcrumbs response matches BreadcrumbsResponse schema."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-iphone"})

    assert resp.status_code == 200
    data = resp.json()

    assert "items" in data
    assert isinstance(data["items"], list)

    item = data["items"][0]
    assert "category_id" in item
    assert "name" in item
    assert "parent_id" in item
