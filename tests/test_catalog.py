"""Tests for catalog endpoints — US-CAT-01: каталог с фильтрами и фасетами.

Covers:
  - catalog_returns_filtered_sorted_products   (happy path)
  - facets_return_counts_per_filter_value      (facets)
  - invalid_sort_returns_400                   (bad sort field)
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
# catalog.py does: from src.services.b2b_client import b2b_client
# So we must patch src.routes.catalog.b2b_client, NOT src.services.b2b_client.b2b_client.

CATALOG_MODULE = "src.routes.catalog.b2b_client"


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
    return {
        "product_id": product_id,
        "title": title,
        "main_image_url": kwargs.get("image", "http://img"),
        "min_price": price,
        "is_available": kwargs.get("available", True),
        "description": kwargs.get("desc", ""),
        "images": [kwargs.get("image", "http://img")],
        "characteristics": kwargs.get("chars", {}),
        "skus": kwargs.get("skus", []),
    }


def _mock_b2b_products_result(products, total=None):
    return {
        "products": products,
        "total": total if total is not None else len(products),
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

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={
                "category_id": "cat1",
                "sort_by": "price",
                "sort_order": "asc",
                "page": 1,
                "page_size": 20,
            },
        )

    assert resp.status_code == 200
    data = resp.json()

    # Pagination
    assert data["total"] == 3
    assert data["page"] == 1
    assert data["page_size"] == 20

    # Products returned
    assert len(data["products"]) == 3

    # Sort order (price asc)
    prices = [p["min_price"] for p in data["products"]]
    assert prices == sorted(prices)

    # Filters applied
    assert data["filters_applied"] is not None
    assert data["filters_applied"]["category_id"] == "cat1"


def test_catalog_returns_filtered_sorted_products_in_stock(client: TestClient):
    """Happy path: in_stock=True filters to available products only."""

    products = [
        _make_product("p1", "Available", 50.0, available=True),
    ]

    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"in_stock": True, "page": 1, "page_size": 10},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["products"]) == 1
    assert data["products"][0]["is_available"] is True

    # Verify in_stock was passed through
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["in_stock"] is True


def test_catalog_price_range_filter(client: TestClient):
    """Happy path: min_price / max_price range filter."""

    products = [
        _make_product("p1", "Mid product", 150.0),
    ]

    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"min_price": 100, "max_price": 200, "page": 1, "page_size": 10},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["min_price"] == 100
    assert call_kwargs["max_price"] == 200


def test_catalog_search_filter(client: TestClient):
    """Happy path: search by query text."""

    products = [
        _make_product("p1", "Wireless Mouse", 25.0),
    ]

    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"search": "mouse", "page": 1, "page_size": 10},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["search"] == "mouse"


def test_catalog_brand_filter(client: TestClient):
    """Happy path: brand slug filter."""

    products = [_make_product("p1", "Apple Phone", 999.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"brand": "apple", "page": 1, "page_size": 10},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["brand"] == "apple"


def test_catalog_empty_results(client: TestClient):
    """Happy path: no products match → empty list, total=0."""

    mock_result = _mock_b2b_products_result([], total=0)

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"category_id": "nonexistent", "page": 1, "page_size": 10},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert len(data["products"]) == 0


def test_catalog_page_size_limit(client: TestClient):
    """Edge: page_size=100 accepted, page_size=101 rejected (422)."""

    products = [_make_product("p1", "X", 10.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"page_size": 100, "page": 1},
        )

    assert resp.status_code == 200

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"page_size": 101, "page": 1},
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

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_facets = AsyncMock(return_value=_mock_b2b_facets_result(facets_data))

        resp = client.get(
            "/api/v1/catalog/facets",
            params={"category_id": "cat1"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["category_id"] == "cat1"
    assert len(data["facets"]) == 4

    # Verify counts preserved
    brand_facet = next(f for f in data["facets"] if f["name"] == "brand")
    assert len(brand_facet["values"]) == 2
    assert brand_facet["values"][0]["count"] == 45
    assert brand_facet["values"][1]["count"] == 67


def test_facets_with_price_range_filter(client: TestClient):
    """Facets respect min_price / max_price filter params."""

    facets_data = {
        "brand": [
            {"value": "apple", "label": "Apple", "count": 5},
        ],
    }

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_facets = AsyncMock(return_value=_mock_b2b_facets_result(facets_data))

        resp = client.get(
            "/api/v1/catalog/facets",
            params={"min_price": 100, "max_price": 500, "category_id": "cat1"},
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

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_facets = AsyncMock(return_value=_mock_b2b_facets_result(facets_data))

        resp = client.get(
            "/api/v1/catalog/facets",
            params={"in_stock": True, "category_id": "cat1"},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_facets.call_args.kwargs
    assert call_kwargs["in_stock"] is True


# ======================================================================
# TEST 3 — invalid_sort_returns_400
# ======================================================================

def test_invalid_sort_returns_400(client: TestClient):
    """Invalid sort_by returns 400 with list of allowed values."""

    products = [_make_product("p1", "X", 10.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"sort_by": "nonexistent_field", "page": 1, "page_size": 10},
        )

    assert resp.status_code == 400
    data = resp.json()
    assert data["detail"]["error"] == "INVALID_SORT_FIELD"
    assert "nonexistent_field" in data["detail"]["message"]
    assert "price" in data["detail"]["allowed"]
    assert "title" in data["detail"]["allowed"]
    assert "popularity" in data["detail"]["allowed"]


def test_invalid_sort_order_returns_400(client: TestClient):
    """Invalid sort_order returns 400."""

    products = [_make_product("p1", "X", 10.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"sort_by": "price", "sort_order": "invalid", "page": 1, "page_size": 10},
        )

    assert resp.status_code == 400
    data = resp.json()
    assert data["detail"]["error"] == "INVALID_SORT_ORDER"
    assert "asc" in data["detail"]["allowed"]
    assert "desc" in data["detail"]["allowed"]


# ======================================================================
# TEST 4 — b2b_unavailable_returns_502
# ======================================================================

def test_b2b_unavailable_returns_502_products(client: TestClient):
    """When B2B is unavailable, GET /products returns 502."""

    from src.services.b2b_client import B2BClientError

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(
            side_effect=B2BClientError(status_code=503, message="Service Unavailable")
        )

        resp = client.get(
            "/api/v1/catalog/products",
            params={"category_id": "cat1", "page": 1, "page_size": 10},
        )

    assert resp.status_code == 502
    data = resp.json()
    assert data["detail"]["error"] == "B2B_UNAVAILABLE"
    assert "Service Unavailable" in data["detail"]["message"]


def test_b2b_unavailable_returns_502_facets(client: TestClient):
    """When B2B is unavailable, GET /facets returns 502."""

    from src.services.b2b_client import B2BClientError

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_facets = AsyncMock(
            side_effect=B2BClientError(status_code=503, message="Service Unavailable")
        )

        resp = client.get(
            "/api/v1/catalog/facets",
            params={"category_id": "cat1"},
        )

    assert resp.status_code == 502
    data = resp.json()
    assert data["detail"]["error"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_returns_502_product_detail(client: TestClient):
    """When B2B is unavailable, GET /products/{id} returns 502."""

    from src.services.b2b_client import B2BClientError

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(
            side_effect=B2BClientError(status_code=503, message="Service Unavailable")
        )

        resp = client.get("/api/v1/catalog/products/some_id")

    assert resp.status_code == 502
    data = resp.json()
    assert data["detail"]["error"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_returns_502_categories(client: TestClient):
    """When B2B is unavailable, GET /categories returns 502."""

    from src.services.b2b_client import B2BClientError

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(
            side_effect=B2BClientError(status_code=503, message="Service Unavailable")
        )

        resp = client.get("/api/v1/catalog/categories")

    assert resp.status_code == 502


# ======================================================================
# TEST 5 — product not found (404)
# ======================================================================

def test_product_not_found_returns_404(client: TestClient):
    """Non-existent product returns 404."""

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=None)

        resp = client.get("/api/v1/catalog/products/nonexistent")

    assert resp.status_code == 404
    data = resp.json()
    assert data["detail"]["error"] == "PRODUCT_NOT_FOUND"


# ======================================================================
# TEST 6 — product detail returns full data
# ======================================================================

def test_get_product_detail_returns_full_data(client: TestClient):
    """GET /products/{id} returns full product detail with images and characteristics."""

    product = {
        "product_id": "p1",
        "title": "Full Product",
        "main_image_url": "http://main.jpg",
        "min_price": 999.99,
        "is_available": True,
        "description": "Full description",
        "images": ["http://img1.jpg", "http://img2.jpg"],
        "characteristics": {"color": "red", "size": "L"},
        "skus": [
            {
                "sku_id": "s1",
                "color": "red",
                "size": "L",
                "price": 999.99,
                "quantity_available": 5,
                "is_active": True,
            }
        ],
    }

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_product_by_id = AsyncMock(return_value=product)

        resp = client.get("/api/v1/catalog/products/p1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["product_id"] == "p1"
    assert data["title"] == "Full Product"
    assert data["min_price"] == 999.99
    assert len(data["images"]) == 2
    assert data["characteristics"]["color"] == "red"
    assert len(data["skus"]) == 1
    assert data["skus"][0]["sku_id"] == "s1"


# ======================================================================
# TEST 7 — categories endpoint
# ======================================================================

def test_get_categories(client: TestClient):
    """GET /categories returns category tree from B2B."""

    categories = [
        {"category_id": "cat1", "name": "Electronics", "parent_id": None},
        {"category_id": "cat2", "name": "Phones", "parent_id": "cat1"},
    ]

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_categories = AsyncMock(return_value=categories)

        resp = client.get("/api/v1/catalog/categories")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["category_id"] == "cat1"


# ======================================================================
# TEST 8 — X-Service-Key header
# ======================================================================

def test_b2b_client_sends_x_service_key():
    """B2B client includes X-Service-Key header on requests."""

    from src.services.b2b_client import B2BClient

    client = B2BClient()
    headers = client._headers()
    assert "X-Service-Key" in headers
    assert headers["X-Service-Key"] == "neomarket-b2c-secret-key"


# ======================================================================
# TEST 9 — default sort values
# ======================================================================

def test_default_sort_fields(client: TestClient):
    """Default sort_by=popularity, sort_order=desc."""

    products = [_make_product("p1", "X", 10.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={"page": 1, "page_size": 10},
        )

    assert resp.status_code == 200
    call_kwargs = mock_b2b.get_products.call_args.kwargs
    assert call_kwargs["sort_by"] == "popularity"
    assert call_kwargs["sort_order"] == "desc"


# ======================================================================
# TEST 10 — facets with dict-style response from B2B
# ======================================================================

def test_facets_dict_response_from_b2b(client: TestClient):
    """Facets handle dict-style response from B2B (fallback format)."""

    facets_data = {
        "brand": {"apple": 45, "samsung": 67, "xiaomi": 34},
    }

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_facets = AsyncMock(return_value=_mock_b2b_facets_result(facets_data))

        resp = client.get("/api/v1/catalog/facets", params={"category_id": "cat1"})

    assert resp.status_code == 200
    data = resp.json()
    brand_facet = next(f for f in data["facets"] if f["name"] == "brand")
    assert len(brand_facet["values"]) == 3
    assert brand_facet["values"][0]["count"] == 45
    assert brand_facet["values"][1]["count"] == 67


# ======================================================================
# TEST 11 — facets empty
# ======================================================================

def test_facets_empty_result(client: TestClient):
    """Facets with empty B2B response returns empty facets list."""

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_facets = AsyncMock(return_value={"facets": {}})

        resp = client.get("/api/v1/catalog/facets", params={"category_id": "cat1"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["category_id"] == "cat1"
    assert data["facets"] == []


# ======================================================================
# TEST 12 — combined filters
# ======================================================================

def test_combined_filters(client: TestClient):
    """Multiple filters combined and passed to B2B correctly."""

    products = [_make_product("p1", "X", 150.0)]
    mock_result = _mock_b2b_products_result(products, total=1)

    with patch(CATALOG_MODULE) as mock_b2b:
        mock_b2b.get_products = AsyncMock(return_value=mock_result)

        resp = client.get(
            "/api/v1/catalog/products",
            params={
                "category_id": "cat1",
                "search": "phone",
                "min_price": 100,
                "max_price": 200,
                "in_stock": True,
                "brand": "apple",
                "sort_by": "price",
                "sort_order": "desc",
                "page": 2,
                "page_size": 50,
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
    assert call_kwargs["page"] == 2
    assert call_kwargs["page_size"] == 50
