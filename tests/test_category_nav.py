"""Tests for category navigation endpoints — US-CAT-05.

Covers the canon flow ``flows/b2c-catalog-flows.md#b2c-5-category-nav``:

  - category_tree_returns_nested_structure   (tree rebuilt from a flat list)
  - breadcrumbs_return_path_from_root        (chain from root to category)
  - unknown_category_returns_404             (unknown category → 404)
  - orphan_node_returns_422                  (broken hierarchy → 422)
  - ambiguous_params_returns_400             (both category_id & product_id → 400)

Plus the surrounding contract: flat ``CategoryRef`` list, single-category
lookup, product-based breadcrumbs, missing params, cycles, B2B outages, and a
strict read of the published B2B ``CategoryResponse`` payload (``id`` /
``level`` / ``path`` — not ``category_id``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.b2b_client import B2BClient, B2BClientError
from src.services.b2b_public_catalog import B2BCategory, B2BProduct

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
# Helpers — B2B ``CategoryResponse`` objects
# ======================================================================

def _category(
    category_id: str,
    name: str,
    parent_id: str | None,
    level: int,
    path: str,
) -> B2BCategory:
    """One B2B ``CategoryResponse`` (id/name/parent_id/level/path/is_active/...)."""
    return B2BCategory(
        id=category_id,
        name=name,
        parent_id=parent_id,
        level=level,
        path=path,
        is_active=True,
        created_at="2026-01-01T00:00:00Z",
    )


def _flat_categories() -> list[B2BCategory]:
    """A flat adjacency list forming this tree:

    Electronics (cat-root)
    ├── Phones (cat-phones, parent: cat-root)
    │   ├── iPhone (cat-iphone, parent: cat-phones)
    │   └── Samsung (cat-samsung, parent: cat-phones)
    └── Laptops (cat-laptops, parent: cat-root)
    """
    return [
        _category("cat-root", "Electronics", None, 0, "electronics"),
        _category("cat-phones", "Phones", "cat-root", 1, "electronics/phones"),
        _category("cat-iphone", "iPhone", "cat-phones", 2, "electronics/phones/iphone"),
        _category("cat-samsung", "Samsung", "cat-phones", 2, "electronics/phones/samsung"),
        _category("cat-laptops", "Laptops", "cat-root", 1, "electronics/laptops"),
    ]


def _b2b_error_mock(message: str = "Service Unavailable"):
    """Create an async mock that raises B2BClientError."""

    async def _raise(*args, **kwargs):
        raise B2BClientError(status_code=503, message=message)

    return _raise


def _b2b_product(category_id: str = "cat-iphone", status: str = "MODERATED") -> B2BProduct:
    return B2BProduct(
        id="p1",
        seller_id="s1",
        category_id=category_id,
        title="iPhone 15",
        slug="iphone-15",
        description="",
        status=status,
        images=[],
        characteristics=[],
        skus=[],
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


# ======================================================================
# TEST 1 — category_tree_returns_nested_structure
# ======================================================================

def test_category_tree_returns_nested_structure(client: TestClient):
    """Tree is rebuilt from B2B's flat adjacency list into nested children."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/categories/tree")

    assert resp.status_code == 200
    data = resp.json()

    # Only Electronics is a root.
    assert len(data) == 1

    electronics = data[0]
    assert electronics["id"] == "cat-root"
    assert electronics["name"] == "Electronics"
    assert electronics["parent_id"] is None
    assert electronics["level"] == 0
    assert electronics["path"] == ["Electronics"]
    assert len(electronics["children"]) == 2  # Phones, Laptops

    phones = next(c for c in electronics["children"] if c["id"] == "cat-phones")
    assert phones["parent_id"] == "cat-root"
    assert phones["level"] == 1
    assert phones["path"] == ["Electronics", "Phones"]
    assert len(phones["children"]) == 2  # iPhone, Samsung

    iphone = next(c for c in phones["children"] if c["id"] == "cat-iphone")
    assert iphone["level"] == 2
    assert iphone["path"] == ["Electronics", "Phones", "iPhone"]
    assert iphone["children"] == []

    laptops = next(c for c in electronics["children"] if c["id"] == "cat-laptops")
    assert laptops["children"] == []


def test_category_tree_empty(client: TestClient):
    """Empty B2B response → empty array, not an envelope."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=[])

        resp = client.get("/api/v1/catalog/categories/tree")

    assert resp.status_code == 200
    assert resp.json() == []


# ======================================================================
# TEST 2 — flat list matches the published CategoryRef schema
# ======================================================================

def test_categories_returns_flat_category_refs(client: TestClient):
    """``GET /categories`` → array of CategoryRef (id/name/level/path)."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/categories")

    assert resp.status_code == 200
    data = resp.json()

    assert isinstance(data, list)
    assert len(data) == 5
    assert {item["id"] for item in data} == {
        "cat-root", "cat-phones", "cat-iphone", "cat-samsung", "cat-laptops",
    }

    iphone = next(item for item in data if item["id"] == "cat-iphone")
    assert iphone["name"] == "iPhone"
    assert iphone["parent_id"] == "cat-phones"
    assert iphone["level"] == 2
    assert iphone["path"] == ["Electronics", "Phones", "iPhone"]


# ======================================================================
# TEST 3 — category_detail_returns_single_category
# ======================================================================

def test_category_detail_returns_single_category(client: TestClient):
    """GET /categories/{id} returns the CategoryRef for one category."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/categories/cat-iphone")

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "cat-iphone"
    assert data["name"] == "iPhone"
    assert data["parent_id"] == "cat-phones"
    assert data["level"] == 2
    assert data["path"] == ["Electronics", "Phones", "iPhone"]


def test_category_detail_root(client: TestClient):
    """Root category detail: no parent, level 0."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/categories/cat-root")

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "cat-root"
    assert data["parent_id"] is None
    assert data["level"] == 0


# ======================================================================
# TEST 4 — unknown_category_returns_404
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
# TEST 5 — breadcrumbs_return_path_from_root
# ======================================================================

def test_breadcrumbs_return_path_from_root(client: TestClient):
    """Breadcrumbs for cat-iphone: Electronics → Phones → iPhone."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-iphone"})

    assert resp.status_code == 200
    data = resp.json()

    assert isinstance(data, list)
    assert [item["id"] for item in data] == ["cat-root", "cat-phones", "cat-iphone"]
    assert [item["name"] for item in data] == ["Electronics", "Phones", "iPhone"]
    assert [item["level"] for item in data] == [0, 1, 2]
    assert data[-1]["path"] == ["Electronics", "Phones", "iPhone"]
    assert data[0]["parent_id"] is None
    assert data[-1]["parent_id"] == "cat-phones"


def test_breadcrumbs_root_category(client: TestClient):
    """Breadcrumbs for a root category is a single item."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-root"})

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "cat-root"
    assert data[0]["path"] == ["Electronics"]


def test_breadcrumbs_level_2_category(client: TestClient):
    """Breadcrumbs for cat-phones: Electronics → Phones."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-phones"})

    assert resp.status_code == 200
    data = resp.json()
    assert [item["id"] for item in data] == ["cat-root", "cat-phones"]
    assert data[-1]["path"] == ["Electronics", "Phones"]


def test_breadcrumbs_response_schema(client: TestClient):
    """Every breadcrumb item matches the published CategoryRef schema."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-iphone"})

    assert resp.status_code == 200
    for item in resp.json():
        assert set(item) == {"id", "name", "parent_id", "level", "path"}
        assert isinstance(item["path"], list)
        assert all(isinstance(part, str) for part in item["path"])


def test_breadcrumbs_unknown_category_returns_404(client: TestClient):
    """Breadcrumbs for a non-existent category → 404."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "ghost-cat"})

    assert resp.status_code == 404
    assert resp.json()["code"] == "CATEGORY_NOT_FOUND"


# ======================================================================
# TEST 6 — breadcrumbs_from_product_id
# ======================================================================

def test_breadcrumbs_from_product_id(client: TestClient):
    """product_id is resolved to its category through the public B2B card."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=_b2b_product())
        mock_b2b.get_categories = AsyncMock(return_value=_flat_categories())

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"product_id": "p1"})

    assert resp.status_code == 200
    data = resp.json()
    assert [item["id"] for item in data] == ["cat-root", "cat-phones", "cat-iphone"]


def test_breadcrumbs_product_not_found_returns_404(client: TestClient):
    """Unknown product → 404 PRODUCT_NOT_FOUND."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=None)

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"product_id": "ghost"})

    assert resp.status_code == 404
    assert resp.json()["code"] == "PRODUCT_NOT_FOUND"


def test_breadcrumbs_hidden_product_returns_404(client: TestClient):
    """A product the storefront must not show is treated as missing."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=_b2b_product(status="BLOCKED"))

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"product_id": "p1"})

    assert resp.status_code == 404
    assert resp.json()["code"] == "PRODUCT_NOT_FOUND"


# ======================================================================
# TEST 7 — ambiguous_params_returns_400
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


# ======================================================================
# TEST 8 — orphan_node_returns_422
# ======================================================================

def test_orphan_node_returns_422(client: TestClient):
    """Category whose parent_id points to a missing category → 422."""
    broken = [_category("cat-orphan", "Orphan", "cat-missing", 1, "orphan")]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=broken)

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-orphan"})

    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "ORPHAN_NODE"
    assert "cat-missing" in data["message"]


def test_orphan_in_middle_returns_422(client: TestClient):
    """Broken hierarchy in the middle of the chain → 422."""
    broken = [
        _category("cat-a", "A", "cat-b", 2, "a"),
        _category("cat-b", "B", "cat-missing", 1, "b"),
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=broken)

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-a"})

    assert resp.status_code == 422
    assert resp.json()["code"] == "ORPHAN_NODE"


def test_tree_orphan_returns_422(client: TestClient):
    """A tree cannot be built truthfully from a broken hierarchy → 422."""
    broken = [_category("cat-orphan", "Orphan", "cat-missing", 1, "orphan")]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=broken)

        resp = client.get("/api/v1/catalog/categories/tree")

    assert resp.status_code == 422
    assert resp.json()["code"] == "ORPHAN_NODE"


# ======================================================================
# TEST 9 — cycle_detected_returns_422
# ======================================================================

def test_cycle_detected_returns_422(client: TestClient):
    """Circular parent reference → 422."""
    cycle = [
        _category("cat-x", "X", "cat-y", 1, "x"),
        _category("cat-y", "Y", "cat-x", 1, "y"),
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=cycle)

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-x"})

    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "ORPHAN_NODE"
    assert "cycle" in data["message"].lower()


# ======================================================================
# TEST 10 — B2B unavailable → 502
# ======================================================================

def test_b2b_unavailable_tree(client: TestClient):
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = _b2b_error_mock()

        resp = client.get("/api/v1/catalog/categories/tree")

    assert resp.status_code == 502
    assert resp.json()["code"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_category_list(client: TestClient):
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = _b2b_error_mock()

        resp = client.get("/api/v1/catalog/categories")

    assert resp.status_code == 502
    assert resp.json()["code"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_category_detail(client: TestClient):
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = _b2b_error_mock()

        resp = client.get("/api/v1/catalog/categories/cat-iphone")

    assert resp.status_code == 502
    assert resp.json()["code"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_breadcrumbs(client: TestClient):
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = _b2b_error_mock()

        resp = client.get("/api/v1/catalog/breadcrumbs", params={"category_id": "cat-iphone"})

    assert resp.status_code == 502
    assert resp.json()["code"] == "B2B_UNAVAILABLE"


# ======================================================================
# TEST 11 — strict B2B payload (published field names only)
# ======================================================================

def _raw_b2b_category(**overrides):
    payload = {
        "id": "cat-root",
        "name": "Electronics",
        "parent_id": None,
        "level": 0,
        "path": "electronics",
        "is_active": True,
        "created_at": "2026-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_category_payload_is_parsed_with_b2b_field_names(client: TestClient):
    """``GET /api/v1/categories`` → CategoryResponse uses ``id``, not ``category_id``."""
    with patch.object(
        B2BClient,
        "_request",
        new=AsyncMock(return_value=[_raw_b2b_category()]),
    ):
        resp = client.get("/api/v1/catalog/categories")

    assert resp.status_code == 200
    assert resp.json()[0]["id"] == "cat-root"


def test_category_payload_with_invented_fields_returns_502(client: TestClient):
    """A payload using ``category_id`` instead of ``id`` is a protocol fault → 502."""
    legacy = _raw_b2b_category()
    legacy["category_id"] = legacy.pop("id")

    with patch.object(
        B2BClient,
        "_request",
        new=AsyncMock(return_value=[legacy]),
    ):
        resp = client.get("/api/v1/catalog/categories")

    assert resp.status_code == 502
    assert resp.json()["code"] == "B2B_UNAVAILABLE"
