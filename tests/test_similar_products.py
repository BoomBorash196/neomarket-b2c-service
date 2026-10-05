"""Tests for GET /api/v1/catalog/products/{product_id}/similar — US-CAT-04.

The B2B side is mocked at the **published client API** level
(``b2b_client.get_public_product``, ``b2b_client.get_public_similar_products``,
``b2b_client.list_public_products``) and the stubbed values are the real
dataclasses from ``src.services.b2b_public_catalog`` — i.e. exactly what the
parsers produce from ``b2b/openapi.yaml``:

* ``GET /public/products/{id}``            → ``ProductPublicResponse``
* ``GET /public/products/{id}/similar``    → ``[ProductPublicShortResponse]``
* ``GET /public/products?category_id=...`` → ``ProductPublicPaginatedResponse``

The previous revision of this file mocked ``get_product_by_id`` and
``get_similar_products(category_id=...)`` / ``{"products": [...]}`` — a B2B
contract that does not exist in the spec. The behaviour asserted here is
unchanged; only the wire shapes are now real.

Covers:
  - similar_returns_up_to_8_from_same_category     (happy path, current excluded)
  - similar_calls_b2b_with_spec_parameters         (no invented category_id)
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
from src.services.b2b_public_catalog import (
    B2BProduct,
    B2BProductPage,
    B2BProductShort,
)

CATALOG_B2B = "src.routes.catalog.b2b_client"

CREATED_AT = "2026-01-15T10:00:00Z"


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def client():
    """Test client with B2B mocked."""
    with TestClient(app=app, raise_server_exceptions=False) as c:
        yield c


# ======================================================================
# Helpers — real dataclasses, exactly as the spec parsers build them
# ======================================================================

def _product(product_id: str, title: str, category_id: str = "cat1") -> B2BProduct:
    """``ProductPublicResponse`` — what ``get_public_product`` returns."""
    return B2BProduct(
        id=product_id,
        seller_id="seller-1",
        category_id=category_id,
        title=title,
        slug=product_id,
        description="",
        status="MODERATED",
        images=[],
        characteristics=[],
        skus=[],
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def _short(
    product_id: str,
    title: str,
    min_price: int = 100,
    category_id: str = "cat1",
) -> B2BProductShort:
    """``ProductPublicShortResponse`` — what the similar endpoint returns."""
    return B2BProductShort(
        id=product_id,
        title=title,
        slug=product_id,
        status="MODERATED",
        category_id=category_id,
        min_price=min_price,
        cover_image="http://img",
        created_at=CREATED_AT,
    )


def _page(items: list[B2BProductShort], limit: int = 50, offset: int = 0) -> B2BProductPage:
    """``ProductPublicPaginatedResponse`` — the parent-category top-up call."""
    return B2BProductPage(items=items, total_count=len(items), limit=limit, offset=offset)


def _category_tree(category_id: str, parent_id: str | None) -> list[dict]:
    """``GET /categories`` payload as consumed by ``_resolve_parent_category``."""
    row = {"category_id": category_id, "name": category_id, "parent_id": parent_id}
    if parent_id:
        return [row, {"category_id": parent_id, "name": parent_id, "parent_id": None}]
    return [row]


# ======================================================================
# TEST 1 — similar_returns_up_to_8_from_same_category
# ======================================================================

def test_similar_returns_up_to_8_from_same_category(client: TestClient):
    """Happy path: up to 8 similar products, current product excluded."""
    current = _product("p-current", "Current Product", category_id="cat1")

    similar_products = [
        _short(f"p-{i}", f"Similar {i}", 100 + i * 10) for i in range(1, 11)
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = AsyncMock(return_value=similar_products)

        resp = client.get("/api/v1/catalog/products/p-current/similar")

    assert resp.status_code == 200
    data = resp.json()

    assert data["current_product_id"] == "p-current"
    assert len(data["recommendations"]) == 8  # capped at 8
    assert data["reason"] == "same_category"

    # Current product must NOT be in results
    rec_ids = [r["id"] for r in data["recommendations"]]
    assert "p-current" not in rec_ids

    # Verify B2B was called with the spec parameters
    call_args = mock_b2b.get_public_similar_products.call_args
    assert call_args.args[0] == "p-current"
    assert call_args.kwargs["limit"] == 9  # 8 + 1, to absorb the current product


def test_similar_calls_b2b_with_spec_parameters(client: TestClient):
    """The published similar operation takes only product_id + limit.

    B2B declares ``GET /public/products/{id}/similar`` with exactly those two
    inputs — there is no ``category_id``, so the route must not send one.
    """
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(
            return_value=_product("p-1", "Product", category_id="cat1")
        )
        mock_b2b.get_public_similar_products = AsyncMock(return_value=[])
        mock_b2b.get_categories = AsyncMock(return_value=_category_tree("cat1", None))

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    mock_b2b.get_public_similar_products.assert_awaited_once()
    kwargs = mock_b2b.get_public_similar_products.call_args.kwargs
    assert "category_id" not in kwargs


def test_similar_returns_fewer_than_8(client: TestClient):
    """Happy path: fewer than 8 available → returns all available."""
    current = _product("p-1", "Product", category_id="cat1")
    similar_products = [
        _short("p-2", "Similar A"),
        _short("p-3", "Similar B"),
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = AsyncMock(return_value=similar_products)
        mock_b2b.get_categories = AsyncMock(return_value=_category_tree("cat1", None))

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
    current = _product("p-1", "Standalone Product", category_id="cat-empty")

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = AsyncMock(return_value=[])
        mock_b2b.get_categories = AsyncMock(return_value=[])
        mock_b2b.list_public_products = AsyncMock(return_value=_page([]))

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    assert data["current_product_id"] == "p-1"
    assert data["recommendations"] == []
    assert len(data["recommendations"]) == 0


def test_empty_category_no_parent_returns_200_empty_list(client: TestClient):
    """No similar + no parent category → 200 with empty list."""
    current = _product("p-1", "Isolated Product", category_id="cat-iso")

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = AsyncMock(return_value=[])
        mock_b2b.get_categories = AsyncMock(return_value=_category_tree("cat-iso", None))
        mock_b2b.list_public_products = AsyncMock(return_value=_page([]))

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    assert data["recommendations"] == []
    assert data["reason"] == "no_similar_products"
    mock_b2b.list_public_products.assert_not_awaited()


# ======================================================================
# TEST 3 — unknown_product_returns_404
# ======================================================================

def test_unknown_product_returns_404(client: TestClient):
    """Non-existent product → 404 PRODUCT_NOT_FOUND."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=None)

        resp = client.get("/api/v1/catalog/products/nonexistent/similar")

    assert resp.status_code == 404
    data = resp.json()
    assert data["code"] == "PRODUCT_NOT_FOUND"
    assert "not found" in data["message"].lower()


# ======================================================================
# TEST 4 — fallback_to_parent_category
# ======================================================================

def test_fallback_to_parent_category(client: TestClient):
    """Not enough in same category → fill from the parent category listing."""
    current = _product("p-1", "Product", category_id="cat-children")

    # Only 2 similar in the same-category selection
    same_cat_similar = [
        _short("p-sim-1", "Same Cat 1", category_id="cat-children"),
        _short("p-sim-2", "Same Cat 2", category_id="cat-children"),
    ]

    # Parent category has more
    parent_cat_items = [
        _short(f"p-parent-{i}", f"Parent Cat {i}", category_id="cat-parent")
        for i in range(1, 8)
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = AsyncMock(return_value=same_cat_similar)
        mock_b2b.get_categories = AsyncMock(
            return_value=_category_tree("cat-children", "cat-parent")
        )
        mock_b2b.list_public_products = AsyncMock(return_value=_page(parent_cat_items))

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()

    # Should have 2 from same + 6 from parent = 8
    assert len(data["recommendations"]) == 8
    assert data["reason"] == "parent_category"

    rec_ids = [r["id"] for r in data["recommendations"]]
    assert "p-sim-1" in rec_ids
    assert "p-sim-2" in rec_ids
    assert "p-parent-1" in rec_ids
    assert "p-parent-6" in rec_ids

    # The top-up goes through the published listing endpoint
    mock_b2b.list_public_products.assert_awaited_once()
    assert (
        mock_b2b.list_public_products.call_args.kwargs["category_id"] == "cat-parent"
    )


def test_no_fallback_when_no_parent_category(client: TestClient):
    """When the category has no parent, no listing top-up is attempted."""
    current = _product("p-1", "Product", category_id="cat1")

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = AsyncMock(return_value=[])
        mock_b2b.get_categories = AsyncMock(return_value=_category_tree("cat1", None))
        mock_b2b.list_public_products = AsyncMock(return_value=_page([]))

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    assert data["recommendations"] == []

    # Similar endpoint queried exactly once, no parent expansion
    mock_b2b.get_public_similar_products.assert_awaited_once()
    mock_b2b.list_public_products.assert_not_awaited()


def test_parent_top_up_failure_is_not_fatal(client: TestClient):
    """A failing parent top-up still returns the same-category selection."""
    from src.services.b2b_client import B2BClientError

    current = _product("p-1", "Product", category_id="cat-children")
    same_cat_similar = [_short("p-sim-1", "Same Cat 1", category_id="cat-children")]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = AsyncMock(return_value=same_cat_similar)
        mock_b2b.get_categories = AsyncMock(
            return_value=_category_tree("cat-children", "cat-parent")
        )
        mock_b2b.list_public_products = AsyncMock(
            side_effect=B2BClientError(status_code=503, message="down", kind="timeout")
        )

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    assert [r["id"] for r in data["recommendations"]] == ["p-sim-1"]
    assert data["reason"] == "same_category"


# ======================================================================
# TEST 5 — current_product_excluded_from_results
# ======================================================================

def test_current_product_excluded_from_results(client: TestClient):
    """Current product must never appear in similar results, even if B2B returns it."""
    current = _product("p-1", "Product", category_id="cat1")

    similar_products = [
        _short("p-1", "Current Product"),  # B2B mistakenly includes current
        _short("p-2", "Similar A"),
        _short("p-3", "Similar B"),
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = AsyncMock(return_value=similar_products)
        mock_b2b.get_categories = AsyncMock(return_value=_category_tree("cat1", None))

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    rec_ids = [r["id"] for r in data["recommendations"]]
    assert "p-1" not in rec_ids
    assert len(data["recommendations"]) == 2  # only non-current ones


def test_current_product_excluded_from_parent_fallback(client: TestClient):
    """Current product must also be excluded during parent category fallback."""
    current = _product("p-1", "Product", category_id="cat-child")

    parent_items = [
        _short("p-1", "Current Product", category_id="cat-parent"),  # mistake
        _short("p-parent-1", "Parent 1", category_id="cat-parent"),
        _short("p-parent-2", "Parent 2", category_id="cat-parent"),
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = AsyncMock(return_value=[])
        mock_b2b.get_categories = AsyncMock(
            return_value=_category_tree("cat-child", "cat-parent")
        )
        mock_b2b.list_public_products = AsyncMock(return_value=_page(parent_items))

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
    """A product present in both selections appears once."""
    current = _product("p-1", "Product", category_id="cat-child")

    same_cat = [
        _short("p-same-1", "Same 1", category_id="cat-child"),
        _short("p-dup", "Dup in both", category_id="cat-child"),
    ]
    parent_items = [
        _short("p-dup", "Dup in both", category_id="cat-parent"),  # duplicate
        _short("p-parent-1", "Parent 1", category_id="cat-parent"),
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = AsyncMock(return_value=same_cat)
        mock_b2b.get_categories = AsyncMock(
            return_value=_category_tree("cat-child", "cat-parent")
        )
        mock_b2b.list_public_products = AsyncMock(return_value=_page(parent_items))

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 200
    data = resp.json()
    rec_ids = [r["id"] for r in data["recommendations"]]
    assert rec_ids.count("p-dup") == 1  # deduplicated
    assert len(data["recommendations"]) == 3


# ======================================================================
# TEST 7 — b2b_unavailable_returns_502
# ======================================================================

def _b2b_error_mock(status_code: int = 503, kind: str = "connect"):
    """Create an async mock that raises B2BClientError."""
    from src.services.b2b_client import B2BClientError

    async def _raise(*args, **kwargs):
        raise B2BClientError(status_code=status_code, message="Service Unavailable", kind=kind)

    return _raise


def test_b2b_unavailable_product_lookup_returns_502(client: TestClient):
    """When B2B is unavailable during product lookup → 502."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = _b2b_error_mock()

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"
    assert "detail" not in data


def test_b2b_unavailable_similar_lookup_returns_502(client: TestClient):
    """When B2B is unavailable during similar lookup → 502."""
    current = _product("p-1", "Product", category_id="cat1")

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = _b2b_error_mock()

        resp = client.get("/api/v1/catalog/products/p-1/similar")

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"
    assert "detail" not in data


# ======================================================================
# TEST 8 — response schema correctness
# ======================================================================

def test_similar_response_schema(client: TestClient):
    """Response matches RecommendationList schema."""
    current = _product("p-1", "Product", category_id="cat1")
    similar_products = [_short("p-2", "Wireless Headphones", min_price=4999)]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=current)
        mock_b2b.get_public_similar_products = AsyncMock(return_value=similar_products)
        mock_b2b.get_categories = AsyncMock(return_value=_category_tree("cat1", None))

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
    assert rec["main_image_url"] == "http://img"
    assert rec["min_price"] == 4999