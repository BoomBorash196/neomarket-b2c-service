"""Tests for catalog endpoints — US-CAT-01: каталог с фильтрами и фасетами.

Covers:
  - catalog_returns_filtered_sorted_products   (happy path)
  - facets_return_counts_per_filter_value      (facets)
  - invalid_sort_returns_400                   (bad sort)
  - b2b_unavailable_returns_502                (B2B down → 502)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app


# ======================================================================
# Patching strategy
# ======================================================================
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
# Helper — build a mocked B2B response
# ======================================================================

def _make_product(product_id: str, title: str, price: float, **kwargs):
    """B2B ProductPublicShortResponse-shaped fixture."""
    return {
        "id": product_id,
        "title": title,
        "cover_image": kwargs.get("image", "http://img"),
        "min_price": price,
        "has_stock": kwargs.get("available", True),
        "description": kwargs.get("desc", ""),
        "images": [kwargs.get("image", "http://img")],
        "characteristics": kwargs.get("chars", {}),
        "skus": kwargs.get("skus", []),
    }


def _mock_b2b_products_result(products, total=None):
    """B2B ProductPublicPaginatedResponse-shaped envelope."""
    return {
        "items": products,
        "total_count": total if total is not None else len(products),
        "limit": 20,
        "offset": 0,
    }


def _mock_b2b_facets_result(facets_dict):
    return {"facets": facets_dict}


# ======================================================================
# TEST 1 — catalog_returns_filtered_sorted_products
# ======================================================================

def test_catalog_returns_filtered_sorted_products(client: TestClient):
    """Happy path: category filter + sort by price asc + pagination work."""
    products = [
        _make_product("p1", "Phone A", 100.0),
        _make_product("p2", "Phone B", 200.0),
        _make_product("p3", "Phone C", 300.0),
    ]

    mock_result = _mock_b2b_products_result(products, total=3)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={
                "filter[category_id]": "cat1",
                "sort": "price_asc",
                "limit": 20,
                "offset": 0,
            },
        )

    assert resp.status_code == 200
    data = resp.json()

    # Pagination — new schema: items, total_count, limit, offset
    assert data["total_count"] == 3
    assert data["limit"] == 20
    assert data["offset"] == 0

    # Products returned
    assert len(data["items"]) == 3

    # Sort order (price asc)
    prices = [p["min_price"] for p in data["items"]]
    assert prices == sorted(prices)

    # Verify params passed through to B2B
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["category_id"] == "cat1"
    assert call_kwargs["sort_by"] == "price"
    assert call_kwargs["sort_order"] == "asc"


def test_catalog_returns_filtered_sorted_products_in_stock(client: TestClient):
    """Happy path: in_stock=True filters to available products only."""
    products = [
        _make_product("p1", "Available", 50.0, available=True),
    ]

    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"filter[in_stock]": "true", "limit": 10, "offset": 0},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["has_stock"] is True

    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["in_stock"] is True


def test_catalog_price_range_filter(client: TestClient):
    """Happy path: min_price / max_price range filter via deepObject."""
    products = [
        _make_product("p1", "Mid product", 150.0),
    ]

    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"filter[price_min]": 100, "filter[price_max]": 200, "limit": 10, "offset": 0},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["min_price"] == 100
    assert call_kwargs["max_price"] == 200


def test_catalog_search_filter(client: TestClient):
    """Happy path: search by query text using `q` param."""
    products = [
        _make_product("p1", "Wireless Mouse", 25.0),
    ]

    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"q": "mouse", "limit": 10, "offset": 0},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["search"] == "mouse"


def test_catalog_brand_filter(client: TestClient):
    """Happy path: brand slug filter via deepObject."""
    products = [_make_product("p1", "Apple Phone", 999.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"filter[brand]": "apple", "limit": 10, "offset": 0},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["brand"] == "apple"


def test_catalog_empty_results(client: TestClient):
    """Happy path: no products match → empty list, total_count=0."""
    mock_result = _mock_b2b_products_result([], total=0)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"filter[category_id]": "nonexistent", "limit": 10, "offset": 0},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 0
    assert len(data["items"]) == 0


def test_catalog_limit_max(client: TestClient):
    """Edge: limit=100 accepted, limit=101 rejected (422)."""
    products = [_make_product("p1", "X", 10.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"limit": 100, "offset": 0},
        )

    assert resp.status_code == 200

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"limit": 101, "offset": 0},
        )

    assert resp.status_code == 422


# ======================================================================
# TEST 2 — facets_return_counts_per_filter_value
# ======================================================================

def test_facets_return_counts_per_filter_value(client: TestClient):
    """Facets endpoint returns correct counts per filter value."""
    facets_data = {
        "brand": [
            {"value": "apple", "label": "Apple", "count": 45},
            {"value": "samsung", "label": "Samsung", "count": 67},
        ],
        "price_range": [
            {"value": "0-50000", "label": "Up to 50,000", "count": 120},
            {"value": "50000-100000", "label": "50,000-100,000", "count": 85},
        ],
        "rating": [
            {"value": "5", "label": "5 stars", "count": 30},
            {"value": "4", "label": "4 stars", "count": 55},
        ],
        "in_stock": [
            {"value": "true", "label": "In stock", "count": 150},
            {"value": "false", "label": "Out of stock", "count": 20},
        ],
    }

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_facets = AsyncMock(return_value=_mock_b2b_facets_result(facets_data))

        resp = client.get(
            "/api/v1/catalog/facets",
            params={"filter[category_id]": "cat1"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["category_id"] == "cat1"
    assert len(data["facets"]) == 4

    brand_facet = next(f for f in data["facets"] if f["name"] == "brand")
    assert len(brand_facet["values"]) == 2
    assert brand_facet["values"][0]["count"] == 45
    assert brand_facet["values"][1]["count"] == 67


def test_facets_with_price_range_filter(client: TestClient):
    """Facets respect price_min / price_max filter params."""
    facets_data = {
        "brand": [
            {"value": "apple", "label": "Apple", "count": 5},
        ],
    }

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_facets = AsyncMock(return_value=_mock_b2b_facets_result(facets_data))

        resp = client.get(
            "/api/v1/catalog/facets",
            params={"filter[price_min]": 100, "filter[price_max]": 500, "filter[category_id]": "cat1"},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_facets.call_args.kwargs
    assert call_kwargs["min_price"] == 100
    assert call_kwargs["max_price"] == 500
    assert call_kwargs["category_id"] == "cat1"


def test_facets_with_in_stock_filter(client: TestClient):
    """Facets respect in_stock=True filter."""
    facets_data = {
        "brand": [
            {"value": "x", "label": "X", "count": 10},
        ],
    }

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_facets = AsyncMock(return_value=_mock_b2b_facets_result(facets_data))

        resp = client.get(
            "/api/v1/catalog/facets",
            params={"filter[in_stock]": "true", "filter[category_id]": "cat1"},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_facets.call_args.kwargs
    assert call_kwargs["in_stock"] is True


# ======================================================================
# TEST 3 — invalid_sort_returns_400
# ======================================================================

def test_invalid_sort_returns_400(client: TestClient):
    """Invalid sort returns 400 with list of allowed values."""
    products = [_make_product("p1", "X", 10.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"sort": "nonexistent", "limit": 10, "offset": 0},
        )

    assert resp.status_code == 400
    data = resp.json()
    assert data["code"] == "INVALID_SORT"
    assert "nonexistent" in data["message"]
    assert "price_asc" in data["message"]
    assert "price_desc" in data["message"]
    assert "popularity" in data["message"]
    assert "new" in data["message"]


def test_sort_price_asc(client: TestClient):
    """Sort=price_asc maps to sort_by=price, sort_order=asc for B2B."""
    products = [_make_product("p1", "X", 10.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"sort": "price_asc", "limit": 10, "offset": 0},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["sort_by"] == "price"
    assert call_kwargs["sort_order"] == "asc"


def test_sort_new(client: TestClient):
    """Sort=new maps to sort_by=created_at, sort_order=desc for B2B."""
    products = [_make_product("p1", "X", 10.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"sort": "new", "limit": 10, "offset": 0},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["sort_by"] == "created_at"
    assert call_kwargs["sort_order"] == "desc"


def test_default_sort_is_popularity(client: TestClient):
    """Default sort (no sort param) is popularity desc."""
    products = [_make_product("p1", "X", 10.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"limit": 10, "offset": 0},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["sort_by"] == "popularity"
    assert call_kwargs["sort_order"] == "desc"


def _b2b_error_mock(message: str = "Service Unavailable"):
    """Create an async mock that raises B2BClientError."""
    from src.services.b2b_client import B2BClientError

    async def _raise(*args, **kwargs):
        raise B2BClientError(status_code=503, message=message)

    return _raise


# ======================================================================
# TEST 4 — b2b_unavailable_returns_502
# ======================================================================

def test_b2b_unavailable_returns_502_products(client: TestClient):
    """When B2B is unavailable, GET /products returns 502."""
    with patch("src.routes.catalog.b2b_client.get_products", _b2b_error_mock()):
        resp = client.get(
            "/api/v1/catalog/products",
            params={"filter[category_id]": "cat1", "limit": 10, "offset": 0},
        )

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"
    assert "Service Unavailable" in data["message"]


def test_b2b_unavailable_returns_502_facets(client: TestClient):
    """When B2B is unavailable, GET /facets returns 502."""
    with patch("src.routes.catalog.b2b_client.get_facets", _b2b_error_mock()):
        resp = client.get(
            "/api/v1/catalog/facets",
            params={"filter[category_id]": "cat1"},
        )

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_returns_502_product_detail(client: TestClient):
    """When B2B is unavailable, GET /products/{id} returns 502."""
    with patch("src.routes.catalog.b2b_client.get_product_by_id", _b2b_error_mock()):
        resp = client.get("/api/v1/catalog/products/some_id")

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_returns_502_categories(client: TestClient):
    """When B2B is unavailable, GET /categories returns 502."""
    with patch("src.routes.catalog.b2b_client.get_categories", _b2b_error_mock()):
        resp = client.get("/api/v1/catalog/categories")

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"


# ======================================================================
# TEST 5 — product not found (404)
# ======================================================================

def test_product_not_found_returns_404(client: TestClient):
    """Non-existent or blocked product returns 404."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=None)

        resp = client.get("/api/v1/catalog/products/nonexistent")

    assert resp.status_code == 404
    data = resp.json()
    assert data["code"] == "PRODUCT_NOT_FOUND"


def test_blocked_product_returns_404(client: TestClient):
    """Blocked / deleted product returns 404."""
    with patch(CATALOG_B2B) as mock_b2b:
        # B2B returns None for blocked products
        mock_b2b.get_product_by_id = AsyncMock(return_value=None)

        resp = client.get("/api/v1/catalog/products/blocked-product")

    assert resp.status_code == 404
    data = resp.json()
    assert data["code"] == "PRODUCT_NOT_FOUND"


# ======================================================================
# TEST 6 — product card happy path
# ======================================================================

def test_product_card_returns_full_data_with_skus(client: TestClient):
    """Happy path: product card returns full data with SKUs."""
    product = {
        "id": "p1",
        "title": "Wireless Headphones",
        "cover_image": "http://main.jpg",
        "min_price": 4999.0,
        "has_stock": True,
        "description": "High-quality wireless headphones with ANC.",
        "images": [
            {"id": "i1", "url": "http://img1.jpg", "ordering": 0},
            {"id": "i2", "url": "http://img2.jpg", "ordering": 1},
            {"id": "i3", "url": "http://img3.jpg", "ordering": 2},
        ],
        "characteristics": [{"name": "color", "value": "black"}, {"name": "bluetooth", "value": "5.0"}],
        "skus": [
            {
                "id": "s1-black",
                "color": "black",
                "size": None,
                "price": 4999.0,
                "active_quantity": 10,
                "is_active": True,
                "discount": 500.0,
            },
            {
                "id": "s2-white",
                "color": "white",
                "size": None,
                "price": 5299.0,
                "active_quantity": 3,
                "is_active": True,
                "discount": 0.0,
            },
        ],
    }

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=product)

        resp = client.get("/api/v1/catalog/products/p1")

    assert resp.status_code == 200
    data = resp.json()

    assert data["id"] == "p1"
    assert data["name"] == "Wireless Headphones"
    assert data["min_price"] == 4999.0
    assert len(data["images"]) == 3
    assert data["characteristics"]["color"] == "black"
    assert data["description"] == "High-quality wireless headphones with ANC."

    assert len(data["skus"]) == 2

    sku1 = next(s for s in data["skus"] if s["id"] == "s1-black")
    assert sku1["color"] == "black"
    assert sku1["price"] == 4999.0
    assert sku1["available_quantity"] == 10
    assert sku1["is_active"] is True
    assert sku1["in_stock"] is True
    assert sku1["discount"] == 500.0

    sku2 = next(s for s in data["skus"] if s["id"] == "s2-white")
    assert sku2["price"] == 5299.0
    assert sku2["discount"] == 0.0


# ======================================================================
# TEST 7 — cost_price MUST NOT appear in response
# ======================================================================

def test_cost_price_absent_in_response(client: TestClient):
    """CRITICAL: cost_price must never appear in B2C SKU response."""
    product = {
        "id": "p1",
        "title": "Test Product",
        "cover_image": "http://img.jpg",
        "min_price": 1000.0,
        "has_stock": True,
        "description": "Test",
        "images": [],
        "characteristics": [],
        "skus": [
            {
                "id": "s1",
                "color": "red",
                "price": 1000.0,
                "active_quantity": 5,
                "is_active": True,
                "cost_price": 250.0,
                "reserved_quantity": 2,
                "internal_note": "secret",
                "margin": 3.0,
            },
        ],
    }

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=product)

        resp = client.get("/api/v1/catalog/products/p1")

    assert resp.status_code == 200
    data = resp.json()
    sku = data["skus"][0]

    assert "cost_price" not in sku, "cost_price LEAKED — security violation!"
    assert "reserved_quantity" not in sku, "reserved_quantity LEAKED — security violation!"
    assert "internal_note" not in sku, "internal_note LEAKED — security violation!"
    assert "margin" not in sku, "margin LEAKED — security violation!"

    assert sku["id"] == "s1"
    assert sku["price"] == 1000.0
    assert sku["available_quantity"] == 5
    assert sku["is_active"] is True


# ======================================================================
# TEST 8 — SKU without stock is shown as unavailable (not hidden)
# ======================================================================

def test_sku_without_stock_is_shown_as_unavailable(client: TestClient):
    """SKU with quantity_available = 0 is returned but with in_stock = false."""
    product = {
        "id": "p1",
        "title": "Limited Product",
        "cover_image": "http://img.jpg",
        "min_price": 2000.0,
        "has_stock": True,
        "description": "Only some SKUs in stock",
        "images": [],
        "characteristics": [],
        "skus": [
            {
                "id": "s1-in-stock",
                "color": "red",
                "price": 2000.0,
                "active_quantity": 5,
                "is_active": True,
            },
            {
                "id": "s2-out-of-stock",
                "color": "blue",
                "price": 2200.0,
                "active_quantity": 0,
                "is_active": True,
            },
        ],
    }

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=product)

        resp = client.get("/api/v1/catalog/products/p1")

    assert resp.status_code == 200
    data = resp.json()

    assert len(data["skus"]) == 2

    sku_in = next(s for s in data["skus"] if s["id"] == "s1-in-stock")
    assert sku_in["available_quantity"] == 5
    assert sku_in["in_stock"] is True

    sku_out = next(s for s in data["skus"] if s["id"] == "s2-out-of-stock")
    assert sku_out["available_quantity"] == 0
    assert sku_out["in_stock"] is False


# ======================================================================
# TEST 9 — categories endpoint
# ======================================================================

def test_get_categories(client: TestClient):
    """GET /categories returns category tree from B2B."""
    categories = [
        {"id": "cat1", "name": "Electronics", "parent_id": None},
        {"id": "cat2", "name": "Phones", "parent_id": "cat1"},
    ]

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=categories)

        resp = client.get("/api/v1/catalog/categories")

    assert resp.status_code == 200
    data = resp.json()
    # Response is CategoryListResponse: {items: [...]}
    assert len(data["items"]) == 2
    assert data["items"][0]["category_id"] == "cat1"


# ======================================================================
# TEST 10 — X-Service-Key header
# ======================================================================

def test_b2b_client_sends_x_service_key():
    """B2B client includes X-Service-Key header on requests."""
    from src.services.b2b_client import B2BClient

    client = B2BClient()
    headers = client._headers()
    assert "X-Service-Key" in headers
    assert headers["X-Service-Key"] == "neomarket-b2c-secret-key"


# ======================================================================
# TEST 11 — facets with dict-style response from B2B
# ======================================================================

def test_facets_dict_response_from_b2b(client: TestClient):
    """Facets handle dict-style response from B2B (fallback format)."""
    facets_data = {
        "brand": {"apple": 45, "samsung": 67, "xiaomi": 34},
    }

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_facets = AsyncMock(return_value=_mock_b2b_facets_result(facets_data))

        resp = client.get("/api/v1/catalog/facets", params={"filter[category_id]": "cat1"})

    assert resp.status_code == 200
    data = resp.json()
    brand_facet = next(f for f in data["facets"] if f["name"] == "brand")
    assert len(brand_facet["values"]) == 3
    assert brand_facet["values"][0]["count"] == 45
    assert brand_facet["values"][1]["count"] == 67


# ======================================================================
# TEST 12 — facets empty
# ======================================================================

def test_facets_empty_result(client: TestClient):
    """Facets with empty B2B response returns empty facets list."""
    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_facets = AsyncMock(return_value={"facets": {}})

        resp = client.get("/api/v1/catalog/facets", params={"filter[category_id]": "cat1"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["category_id"] == "cat1"
    assert data["facets"] == []


# ======================================================================
# TEST 13 — combined filters
# ======================================================================

def test_combined_filters(client: TestClient):
    """Multiple filters combined and passed to B2B correctly."""
    products = [_make_product("p1", "X", 150.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={
                "filter[category_id]": "cat1",
                "q": "phone",
                "filter[price_min]": 100,
                "filter[price_max]": 200,
                "filter[in_stock]": "true",
                "filter[brand]": "apple",
                "sort": "price_desc",
                "limit": 50,
                "offset": 50,
            },
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["category_id"] == "cat1"
    assert call_kwargs["search"] == "phone"
    assert call_kwargs["min_price"] == 100
    assert call_kwargs["max_price"] == 200
    assert call_kwargs["in_stock"] is True
    assert call_kwargs["brand"] == "apple"
    assert call_kwargs["sort_by"] == "price"
    assert call_kwargs["sort_order"] == "desc"
    # Pagination goes to B2B as limit/offset per B2B OpenAPI
    assert call_kwargs["limit"] == 50
    assert call_kwargs["offset"] == 50


# ======================================================================
# US-CAT-02 — Text search for products
# ======================================================================

def test_search_returns_matching_products(client: TestClient):
    """Happy path: search matches products by title and description."""
    products = [
        _make_product("p1", "Wireless Mouse", 25.0),
        _make_product("p2", "Gaming Mouse Pro", 75.0),
    ]

    mock_result = _mock_b2b_products_result(products, total=2)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"q": "mouse", "limit": 10, "offset": 0},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 2
    assert len(data["items"]) == 2

    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["search"] == "mouse"


def test_short_query_returns_400(client: TestClient):
    """Query shorter than 3 characters → 400 SHORT_SEARCH_QUERY."""
    resp = client.get(
        "/api/v1/catalog/products",
        params={"q": "ab", "limit": 10, "offset": 0},
    )

    assert resp.status_code == 400
    data = resp.json()
    assert data["code"] == "SHORT_SEARCH_QUERY"
    assert "3" in data["message"]


def test_special_chars_do_not_break_query(client: TestClient):
    """Special characters (% _ ') are escaped and do not break the query."""
    products = [_make_product("p1", "iPhone%15", 999.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        # All three SQL metacharacters in one query
        resp = client.get(
            "/api/v1/catalog/products",
            params={"q": "iPhone%15' and 1=1", "limit": 10, "offset": 0},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1

    call_kwargs = mock_b2b.get_products.call_args.kwargs
    search_arg = call_kwargs["search"]
    # Each special char must be backslash-escaped
    assert "%" in search_arg or "\\%" in search_arg
    assert "'" in search_arg or "\\'" in search_arg


def test_empty_results_returns_200(client: TestClient):
    """No matching products → 200 with an empty items list."""
    mock_result = _mock_b2b_products_result([], total=0)

    with patch(CATALOG_B2B) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"q": "zzzznonexistentzzzz", "limit": 10, "offset": 0},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 0
    assert len(data["items"]) == 0