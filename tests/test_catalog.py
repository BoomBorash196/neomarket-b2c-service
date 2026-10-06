"""Tests for catalog endpoints — US-CAT-01: каталог с фильтрами и фасетами.

Every B2B fixture in this file is a literal transcription of the published B2B
contract (``b2b/openapi.yaml``, tag ``Public Catalog``):

* ``GET /public/products``            → ``ProductPublicPaginatedResponse``
* ``GET /public/products/{id}``       → ``ProductPublicResponse``
* ``POST /public/products/batch``     → ``[ProductPublicResponse]``

If the implementation diverges from those schemas, these tests fail — the
fixtures do not encode an invented wrapper or invented product fields.

Covers:
  - catalog_returns_filtered_sorted_products   (happy path)
  - facets_return_counts_per_filter_value     (facets)
  - invalid_sort_returns_400                  (bad sort)
  - b2b_unavailable_returns_502               (B2B down → 502)
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.b2b_client import b2b_client, B2BClientError

# ======================================================================
# Patching strategy
# ======================================================================
# Catalog routes and the facet service both hold a reference to the singleton,
# so each is patched where it is used.
CATALOG_B2B = "src.routes.catalog.b2b_client"
FACET_B2B = "src.services.facet_service.b2b_client"


# ======================================================================
# Fixtures
# ======================================================================
@pytest.fixture(autouse=True)
def _clear_facet_cache():
    """Facet counters are memoised — every test starts from a cold cache."""
    from src.services import facet_service

    facet_service.clear_facet_cache()
    yield
    facet_service.clear_facet_cache()


@pytest.fixture
def client():
    """Test client with B2B mocked."""
    with TestClient(app=app, raise_server_exceptions=False) as c:
        yield c


class FakeB2BClient:
    """Stand-in for the B2B singleton shared by the catalog routes and the facet service.

    Assigning an attribute patches the real client for the duration of the
    block, so both call sites observe the same mock::

        with fake_b2b() as b2b:
            b2b.list_public_products = AsyncMock(return_value=page)
            resp = client.get("/api/v1/catalog/products")

    Methods that are never assigned stay auto-specced ``AsyncMock``s.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "_patches", {})

    def __setattr__(self, name: str, value) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._patches[name] = patch.object(b2b_client, name, value)
        self._patches[name].start()
        object.__setattr__(self, name, value)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        auto = AsyncMock(name=f"b2b_client.{name}")
        setattr(self, name, auto)
        return auto

    def stop(self) -> None:
        for patcher in reversed(list(self._patches.values())):
            patcher.stop()
        self._patches.clear()


@contextmanager
def fake_b2b():
    """Context manager yielding a :class:`FakeB2BClient`."""
    fake = FakeB2BClient()
    try:
        yield fake
    finally:
        fake.stop()


# ======================================================================
# B2B payload builders — straight from b2b/openapi.yaml
# ======================================================================
def _b2b_product_short(
    product_id: str,
    title: str,
    min_price: int,
    category_id: str = "11111111-1111-1111-1111-111111111111",
    cover_image: str | None = "https://cdn/img.jpg",
) -> dict:
    """``ProductPublicShortResponse`` — required: id, title, slug, status,
    category_id, min_price, created_at. Optional: cover_image."""
    return {
        "id": product_id,
        "title": title,
        "slug": title.lower().replace(" ", "-"),
        "status": "MODERATED",
        "category_id": category_id,
        "min_price": min_price,
        "cover_image": cover_image,
        "created_at": "2026-01-15T10:00:00Z",
    }


def _b2b_page(items: list[dict], total: int | None = None, limit: int = 20, offset: int = 0) -> dict:
    """``ProductPublicPaginatedResponse`` — required: items, total_count, limit, offset."""
    return {
        "items": items,
        "total_count": total if total is not None else len(items),
        "limit": limit,
        "offset": offset,
    }


def _b2b_sku(
    sku_id: str,
    product_id: str,
    price: int,
    active_quantity: int = 5,
    discount: int = 0,
    article: str | None = "ART-1",
) -> dict:
    """``SKUPublicResponse`` — note the real field names: ``id`` (not ``sku_id``),
    ``active_quantity`` (not ``quantity_available``), no cost_price."""
    return {
        "id": sku_id,
        "product_id": product_id,
        "name": "Default",
        "price": price,
        "discount": discount,
        "stock_quantity": active_quantity + 2,
        "active_quantity": active_quantity,
        "article": article,
        "images": [],
        "characteristics": [],
    }


def _b2b_product(
    product_id: str,
    title: str,
    skus: list[dict] | None = None,
    characteristics: list[dict] | None = None,
    category_id: str = "11111111-1111-1111-1111-111111111111",
) -> dict:
    """``ProductPublicResponse`` — required: id, seller_id, category_id, title,
    slug, description, status, images, characteristics, skus, created_at, updated_at."""
    return {
        "id": product_id,
        "seller_id": "22222222-2222-2222-2222-222222222222",
        "category_id": category_id,
        "title": title,
        "slug": title.lower().replace(" ", "-"),
        "description": f"Описание {title}",
        "status": "MODERATED",
        "images": [
            {"id": "img-1", "url": "https://cdn/main.jpg", "ordering": 0},
            {"id": "img-2", "url": "https://cdn/extra.jpg", "ordering": 1},
        ],
        "characteristics": characteristics
        if characteristics is not None
        else [{"id": "ch-1", "name": "Бренд", "value": "Acme"}],
        "skus": skus if skus is not None else [_b2b_sku("s-1", product_id, 1000)],
        "created_at": "2026-01-15T10:00:00Z",
        "updated_at": "2026-01-16T10:00:00Z",
    }


def _b2b_error(status: int = 503, message: str = "Service Unavailable", kind: str = "http"):
    """Async callable that raises B2BClientError, for use as a patched method."""

    async def _raise(*args, **kwargs):
        raise B2BClientError(status_code=status, message=message, kind=kind)

    return _raise


# ======================================================================
# TEST 1 — catalog_returns_filtered_sorted_products
# ======================================================================
def test_catalog_returns_filtered_sorted_products(client: TestClient):
    """Happy path: category filter + sort + pagination are applied and forwarded."""
    items = [
        _b2b_product_short("p1", "Phone A", 10000),
        _b2b_product_short("p2", "Phone B", 20000),
        _b2b_product_short("p3", "Phone C", 30000),
    ]

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse(items))

        resp = client.get(
            "/api/v1/catalog/products",
            params={
                "filter[category_id]": "11111111-1111-1111-1111-111111111111",
                "sort": "price_asc",
                "limit": 20,
                "offset": 0,
            },
        )

    assert resp.status_code == 200
    data = resp.json()

    assert data["total_count"] == 3
    assert data["limit"] == 20
    assert data["offset"] == 0
    assert len(data["items"]) == 3

    # Card shape per B2C CatalogProductCard
    first = data["items"][0]
    assert set(first) >= {"id", "name", "min_price", "has_stock", "images"}
    assert first["name"] == "Phone A"

    prices = [item["min_price"] for item in data["items"]]
    assert prices == sorted(prices)

    # The B2B call must use the spec's own parameter names
    kwargs = mock_b2b.list_public_products.call_args.kwargs
    assert kwargs["category_id"] == "11111111-1111-1111-1111-111111111111"
    assert kwargs["sort"] == "price_asc"  # B2B enum, not "price"+"asc"
    assert kwargs["limit"] == 20
    assert kwargs["offset"] == 0
    assert "sort_by" not in kwargs
    assert "sort_order" not in kwargs


def _parse(items: list[dict], limit: int = 20, offset: int = 0, total: int | None = None):
    """Turn short fixtures into the parsed page the client returns."""
    from src.services.b2b_public_catalog import parse_product_page

    return parse_product_page(_b2b_page(items, total=total, limit=limit, offset=offset))


def test_catalog_maps_short_response_fields(client: TestClient):
    """Card fields come from ProductPublicShortResponse (title/slug/cover_image)."""
    items = [_b2b_product_short("p1", "Wireless Mouse", 2500)]

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse(items))

        resp = client.get("/api/v1/catalog/products")

    assert resp.status_code == 200
    card = resp.json()["items"][0]
    assert card["id"] == "p1"
    assert card["name"] == "Wireless Mouse"          # from `title`
    assert card["slug"] == "wireless-mouse"          # from `slug`
    assert card["min_price"] == 2500
    assert card["has_stock"] is True                 # B2B public list ⇒ in stock
    assert card["images"][0]["url"] == "https://cdn/img.jpg"  # from `cover_image`
    assert card["images"][0]["is_main"] is True
    # ImageRef requires `id`; B2B gives no id for the cover, so it is derived
    assert card["images"][0]["id"] == "p1-cover"
    assert card["images"][0]["ordering"] == 0


def test_catalog_card_without_cover_image(client: TestClient):
    """`cover_image` is nullable in B2B — card still returns, with no images."""
    items = [_b2b_product_short("p1", "No Image", 1000, cover_image=None)]

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse(items))

        resp = client.get("/api/v1/catalog/products")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["images"] == []


def test_catalog_price_range_filter(client: TestClient):
    """filter[price_min] / filter[price_max] reach B2B as min_price / max_price."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([]))

        resp = client.get(
            "/api/v1/catalog/products",
            params={"filter[price_min]": 100, "filter[price_max]": 200},
        )

    assert resp.status_code == 200
    kwargs = mock_b2b.list_public_products.call_args.kwargs
    assert kwargs["min_price"] == 100
    assert kwargs["max_price"] == 200


def test_catalog_seller_id_filter(client: TestClient):
    """filter[seller_id] is part of the B2C contract and of the B2B operation."""
    seller = "33333333-3333-3333-3333-333333333333"

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([]))

        resp = client.get("/api/v1/catalog/products", params={"filter[seller_id]": seller})

    assert resp.status_code == 200
    assert mock_b2b.list_public_products.call_args.kwargs["seller_id"] == seller


def test_catalog_brand_filter_becomes_b2b_dynamic_filter(client: TestClient):
    """B2C filter[brand] maps onto B2B `filters[brand]` (deepObject)."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([]))

        resp = client.get("/api/v1/catalog/products", params={"filter[brand]": "apple"})

    assert resp.status_code == 200
    kwargs = mock_b2b.list_public_products.call_args.kwargs
    assert kwargs["filters"] == {"brand": "apple"}
    assert "brand" not in kwargs  # not a top-level B2B parameter


def test_catalog_attribute_filter_becomes_b2b_dynamic_filter(client: TestClient):
    """filter[attributes][color]=red → B2B `filters[color]`."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([]))

        resp = client.get(
            "/api/v1/catalog/products",
            params={"filter[attributes][color]": "red", "filter[attributes][size]": "M"},
        )

    assert resp.status_code == 200
    assert mock_b2b.list_public_products.call_args.kwargs["filters"] == {
        "color": "red",
        "size": "M",
    }


def test_catalog_search_filter(client: TestClient):
    """q reaches B2B as `search` (the B2B parameter name)."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([]))

        resp = client.get("/api/v1/catalog/products", params={"q": "mouse"})

    assert resp.status_code == 200
    assert mock_b2b.list_public_products.call_args.kwargs["search"] == "mouse"


def test_catalog_in_stock_true_does_not_filter(client: TestClient):
    """B2B public list is already in-stock-only, so true is a no-op pass-through."""
    items = [_b2b_product_short("p1", "Available", 5000)]

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse(items))

        resp = client.get("/api/v1/catalog/products", params={"filter[in_stock]": "true"})

    assert resp.status_code == 200
    assert resp.json()["items"][0]["has_stock"] is True


def test_catalog_in_stock_false_returns_empty_without_calling_b2b(client: TestClient):
    """`in_stock=false` cannot be served by B2B (it publishes in-stock only) →
    empty selection, and no pointless call to B2B."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([]))

        resp = client.get("/api/v1/catalog/products", params={"filter[in_stock]": "false"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 0
    assert data["items"] == []
    mock_b2b.list_public_products.assert_not_called()


def test_catalog_invalid_in_stock_returns_400(client: TestClient):
    """Unknown filter[in_stock] value is rejected, not silently treated as false."""
    resp = client.get("/api/v1/catalog/products", params={"filter[in_stock]": "maybe"})

    assert resp.status_code == 400
    data = resp.json()
    assert data["code"] == "INVALID_FILTER_VALUE"


def test_catalog_empty_results(client: TestClient):
    """Empty B2B page → 200 with zero items and total_count 0."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([], total=0))

        resp = client.get("/api/v1/catalog/products", params={"limit": 10, "offset": 0})

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 0
    assert data["items"] == []


def test_catalog_pagination_uses_b2b_pagination_envelope(client: TestClient):
    """total_count / limit / offset are echoed from B2B's paginated response."""
    items = [_b2b_product_short(f"p{i}", f"P{i}", 1000 * i) for i in range(3)]

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse(items, limit=3, offset=30, total=57))

        resp = client.get("/api/v1/catalog/products", params={"limit": 3, "offset": 30})

    assert resp.status_code == 200
    data = resp.json()
    assert (data["limit"], data["offset"], data["total_count"]) == (3, 30, 57)
    assert len(data["items"]) == 3


def test_catalog_limit_bounds(client: TestClient):
    """limit is capped at 100 per the B2C/B2B contract; 101 → validation error."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([]))
        assert client.get("/api/v1/catalog/products", params={"limit": 100}).status_code == 200

        resp = client.get("/api/v1/catalog/products", params={"limit": 101})

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "message" in body


def test_catalog_combined_filters(client: TestClient):
    """All filters together reach the B2B call unchanged."""
    category = "11111111-1111-1111-1111-111111111111"

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([]))

        resp = client.get(
            "/api/v1/catalog/products",
            params={
                "filter[category_id]": category,
                "q": "phone",
                "filter[price_min]": 100,
                "filter[price_max]": 200,
                "filter[brand]": "apple",
                "sort": "price_desc",
                "limit": 50,
                "offset": 50,
            },
        )

    assert resp.status_code == 200
    kwargs = mock_b2b.list_public_products.call_args.kwargs
    assert kwargs["category_id"] == category
    assert kwargs["search"] == "phone"
    assert kwargs["min_price"] == 100
    assert kwargs["max_price"] == 200
    assert kwargs["filters"] == {"brand": "apple"}
    assert kwargs["sort"] == "price_desc"
    assert kwargs["limit"] == 50
    assert kwargs["offset"] == 50


# ======================================================================
# TEST 2 — facets_return_counts_per_filter_value
# ======================================================================
def _mock_facet_selection(
    shorts: list[dict],
    cards: dict[str, dict] | None = None,
    total: int | None = None,
):
    """Patch B2B so the facet scan sees exactly this selection."""
    from src.services.b2b_public_catalog import parse_product_page, parse_products

    cards = cards or {}
    cards_by_id: dict[str, object] = {
        pid: parse_products([card], path="products")[0] for pid, card in cards.items()
    }

    async def _list(**kwargs):
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 100)
        page_items = shorts[offset : offset + limit]
        return parse_product_page(
            _b2b_page(page_items, total=total if total is not None else len(shorts),
                      limit=limit, offset=offset)
        )

    async def _batch(product_ids):
        found = [cards[pid] for pid in product_ids if pid in cards]
        return parse_products(found, path="products")

    return _list, _batch


def test_facets_return_counts_per_filter_value(client: TestClient):
    """Facet counts are computed from the selection B2B actually returned."""
    shorts = [
        _b2b_product_short("p1", "A", 25000),
        _b2b_product_short("p2", "B", 75000),
        _b2b_product_short("p3", "C", 150000),
    ]
    cards = {
        "p1": _b2b_product("p1", "A", characteristics=[{"id": "c1", "name": "Бренд", "value": "Acme"}]),
        "p2": _b2b_product("p2", "B", characteristics=[{"id": "c2", "name": "brand", "value": "Globex"}]),
        "p3": _b2b_product("p3", "C", characteristics=[{"id": "c3", "name": "Бренд", "value": "Acme"}]),
    }
    list_mock, batch_mock = _mock_facet_selection(shorts, cards)

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(side_effect=list_mock)
        mock_b2b.batch_public_products = AsyncMock(side_effect=batch_mock)

        resp = client.get("/api/v1/catalog/facets", params={"filter[category_id]": "cat-1"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["category_id"] == "cat-1"

    by_name = {f["name"]: f for f in data["facets"]}

    # brand counts: Acme 2, Globex 1 (sorted by count desc)
    brand = by_name["brand"]
    assert [(v["value"], v["count"]) for v in brand["values"]] == [("acme", 2), ("globex", 1)]

    # price buckets follow min_price in kopecks (250 / 750 / 1500 ₽)
    price = {v["value"]: v["count"] for v in by_name["price_range"]["values"]}
    assert price["0-50000"] == 1          # 25000
    assert price["50000-100000"] == 1     # 75000
    assert price["100000-500000"] == 1    # 150000

    # in_stock: B2B public selection only contains in-stock products
    stock = {v["value"]: v["count"] for v in by_name["in_stock"]["values"]}
    assert stock == {"true": 3}


def test_facets_pass_current_filters_to_b2b(client: TestClient):
    """The current filter context must reach B2B — counts describe that selection."""
    shorts = [_b2b_product_short("p1", "A", 25000)]
    cards = {"p1": _b2b_product("p1", "A")}
    list_mock, batch_mock = _mock_facet_selection(shorts, cards)

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(side_effect=list_mock)
        mock_b2b.batch_public_products = AsyncMock(side_effect=batch_mock)

        resp = client.get(
            "/api/v1/catalog/facets",
            params={
                "filter[category_id]": "cat-1",
                "filter[price_min]": 100,
                "filter[price_max]": 500,
                "filter[brand]": "acme",
                "q": "phone",
            },
        )

    assert resp.status_code == 200
    kwargs = mock_b2b.list_public_products.call_args.kwargs
    assert kwargs["category_id"] == "cat-1"
    assert kwargs["min_price"] == 100
    assert kwargs["max_price"] == 500
    assert kwargs["search"] == "phone"
    assert kwargs["filters"] == {"brand": "acme"}
    assert kwargs["offset"] == 0


def test_facets_without_filters(client: TestClient):
    """No filters → the whole storefront selection is counted."""
    shorts = [_b2b_product_short("p1", "A", 1000)]
    cards = {"p1": _b2b_product("p1", "A", characteristics=[{"id": "c", "name": "brand", "value": "Acme"}])}
    list_mock, batch_mock = _mock_facet_selection(shorts, cards)

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(side_effect=list_mock)
        mock_b2b.batch_public_products = AsyncMock(side_effect=batch_mock)

        resp = client.get("/api/v1/catalog/facets")

    assert resp.status_code == 200
    assert resp.json()["category_id"] == ""
    names = {f["name"] for f in resp.json()["facets"]}
    assert "price_range" in names
    assert "in_stock" in names


def test_facets_in_stock_false_is_empty_selection(client: TestClient):
    """in_stock=false has no B2B counterpart — empty counts, no B2B call."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock()
        mock_b2b.batch_public_products = AsyncMock()

        resp = client.get("/api/v1/catalog/facets", params={"filter[in_stock]": "false"})

    assert resp.status_code == 200
    data = resp.json()
    assert all(facet["values"] == [] for facet in data["facets"])
    mock_b2b.list_public_products.assert_not_called()


def test_facets_brand_absent_when_no_characteristics(client: TestClient):
    """No brand characteristic in B2B data → no brand facet (no invented values)."""
    shorts = [_b2b_product_short("p1", "A", 1000)]
    cards = {"p1": _b2b_product("p1", "A", characteristics=[])}
    list_mock, batch_mock = _mock_facet_selection(shorts, cards)

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(side_effect=list_mock)
        mock_b2b.batch_public_products = AsyncMock(side_effect=batch_mock)

        resp = client.get("/api/v1/catalog/facets")

    assert resp.status_code == 200
    assert "brand" not in {f["name"] for f in resp.json()["facets"]}


def test_facets_scan_is_bounded_and_paged(client: TestClient):
    """Large selections are paged at 100 per B2B call and the scan is capped."""
    from src.services import facet_service

    shorts = [_b2b_product_short(f"p{i}", f"P{i}", 1000 * i) for i in range(250)]
    cards = {f"p{i}": _b2b_product(f"p{i}", f"P{i}") for i in range(250)}
    list_mock, batch_mock = _mock_facet_selection(shorts, cards)

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(side_effect=list_mock)
        mock_b2b.batch_public_products = AsyncMock(side_effect=batch_mock)

        resp = client.get("/api/v1/catalog/facets")

    assert resp.status_code == 200
    assert facet_service.FACET_PAGE_SIZE == 100  # B2B caps limit at 100
    # 250 items → 3 pages of listing + 3 batches of 100 hydration calls
    assert mock_b2b.list_public_products.await_count == 3
    assert mock_b2b.batch_public_products.await_count == 3


def test_facets_are_cached(client: TestClient):
    """Repeated identical facet requests do not re-fan-out to B2B."""
    shorts = [_b2b_product_short("p1", "A", 1000)]
    cards = {"p1": _b2b_product("p1", "A")}
    list_mock, batch_mock = _mock_facet_selection(shorts, cards)

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(side_effect=list_mock)
        mock_b2b.batch_public_products = AsyncMock(side_effect=batch_mock)

        first = client.get("/api/v1/catalog/facets", params={"filter[category_id]": "cat-1"})
        second = client.get("/api/v1/catalog/facets", params={"filter[category_id]": "cat-1"})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert mock_b2b.list_public_products.await_count == 1


def test_facets_different_filters_are_cached_separately(client: TestClient):
    """Cache key includes the filter context — different selections, different counts."""
    from src.services import facet_service

    shorts = [_b2b_product_short("p1", "A", 1000)]
    cards = {"p1": _b2b_product("p1", "A")}
    list_mock, batch_mock = _mock_facet_selection(shorts, cards)

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(side_effect=list_mock)
        mock_b2b.batch_public_products = AsyncMock(side_effect=batch_mock)

        client.get("/api/v1/catalog/facets", params={"filter[category_id]": "cat-1"})
        client.get("/api/v1/catalog/facets", params={"filter[category_id]": "cat-2"})

    assert mock_b2b.list_public_products.await_count == 2
    assert facet_service.FACET_CACHE_TTL_SECONDS > 0


# ======================================================================
# TEST 3 — invalid_sort_returns_400
# ======================================================================
def test_invalid_sort_returns_400(client: TestClient):
    """Invalid sort returns 400 listing the allowed B2C values."""
    resp = client.get("/api/v1/catalog/products", params={"sort": "nonexistent"})

    assert resp.status_code == 400
    data = resp.json()
    assert data["code"] == "INVALID_SORT"
    assert "nonexistent" in data["message"]
    for allowed in ("price_asc", "price_desc", "popularity", "new"):
        assert allowed in data["message"]


def test_invalid_sort_returns_400_on_facets(client: TestClient):
    """Same validation on the facets endpoint."""
    resp = client.get("/api/v1/catalog/facets", params={"sort": "nonexistent"})

    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_SORT"


@pytest.mark.parametrize(
    ("b2c_sort", "b2b_sort"),
    [
        ("price_asc", "price_asc"),
        ("price_desc", "price_desc"),
        ("popularity", "popular"),      # B2B enum spelling
        ("new", "created_desc"),        # B2B enum spelling
    ],
)
def test_b2c_sort_translates_to_b2b_enum(client: TestClient, b2c_sort: str, b2b_sort: str):
    """Every B2C sort value maps onto a value from the B2B sort enum."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([]))

        resp = client.get("/api/v1/catalog/products", params={"sort": b2c_sort})

    assert resp.status_code == 200
    assert mock_b2b.list_public_products.call_args.kwargs["sort"] == b2b_sort


def test_default_sort_is_popularity(client: TestClient):
    """No sort param → popularity, forwarded as the B2B value `popular`."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([]))

        resp = client.get("/api/v1/catalog/products")

    assert resp.status_code == 200
    assert mock_b2b.list_public_products.call_args.kwargs["sort"] == "popular"


# ======================================================================
# TEST 4 — b2b_unavailable_returns_502
# ======================================================================
def test_b2b_unavailable_returns_502(client: TestClient):
    """B2B answering 5xx → 502 B2B_UNAVAILABLE on the catalog listing."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = _b2b_error(503, "Service Unavailable")

        resp = client.get("/api/v1/catalog/products", params={"filter[category_id]": "cat-1"})

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "B2B_UNAVAILABLE"
    assert "Service Unavailable" in data["message"]


def test_b2b_connect_error_returns_502(client: TestClient):
    """Connection refused / DNS failure → 502, not 500."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = _b2b_error(502, "B2B unreachable: ConnectError", kind="connect")

        resp = client.get("/api/v1/catalog/products")

    assert resp.status_code == 502
    assert resp.json()["code"] == "B2B_UNAVAILABLE"


def test_b2b_timeout_returns_502(client: TestClient):
    """Read timeout → 502 (the buyer must not see a 500)."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = _b2b_error(504, "B2B timeout after 10.0s", kind="timeout")

        resp = client.get("/api/v1/catalog/products")

    assert resp.status_code == 502
    assert resp.json()["code"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_returns_502_facets(client: TestClient):
    """B2B down on the facet scan → 502."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = _b2b_error(503, "Service Unavailable")
        mock_b2b.batch_public_products = _b2b_error(503, "Service Unavailable")

        resp = client.get("/api/v1/catalog/facets", params={"filter[category_id]": "cat-1"})

    assert resp.status_code == 502
    assert resp.json()["code"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_returns_502_facets_batch_failure(client: TestClient):
    """A failing hydration call is an upstream outage too → 502, not a 500."""
    from src.services.b2b_public_catalog import parse_product_page

    shorts = [_b2b_product_short("p1", "A", 1000)]

    async def _list(**kwargs):
        return parse_product_page(_b2b_page(shorts, total=1, limit=100, offset=0))

    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(side_effect=_list)
        mock_b2b.batch_public_products = _b2b_error(502, "B2B unreachable: ConnectError", kind="connect")

        resp = client.get("/api/v1/catalog/facets")

    assert resp.status_code == 502
    assert resp.json()["code"] == "B2B_UNAVAILABLE"


def test_b2b_unavailable_returns_502_product_detail(client: TestClient):
    """B2B down on the product card → 502."""
    with fake_b2b() as mock_b2b:
        mock_b2b.get_public_product = _b2b_error(503, "Service Unavailable")

        resp = client.get("/api/v1/catalog/products/p1")

    assert resp.status_code == 502
    assert resp.json()["code"] == "B2B_UNAVAILABLE"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"products": []}, id="invented-list-wrapper"),
        pytest.param({"items": [], "total_count": 0}, id="missing-limit-offset"),
        pytest.param(
            {"items": [], "total_count": 0, "limit": 20, "offset": 0},
            id="empty-selection-is-valid",
        ),
    ],
)
def test_b2b_payload_is_validated_against_spec(client: TestClient, payload: dict):
    """Off-schema B2B bodies are upstream faults, not an empty storefront.

    The first two payloads are the shapes the previous implementation invented
    (a ``products`` wrapper, a paginated envelope without limit/offset). They
    must surface as 502 rather than silently producing an empty catalog.
    """
    with patch.object(b2b_client, "_request", AsyncMock(return_value=payload)):
        resp = client.get("/api/v1/catalog/products")

    if payload.get("items") == [] and {"limit", "offset"} <= set(payload):
        assert resp.status_code == 200
    else:
        assert resp.status_code == 502
        assert resp.json()["code"] == "B2B_UNAVAILABLE"


def test_b2b_product_short_missing_required_field_returns_502(client: TestClient):
    """A product row missing a required B2B field is rejected, not defaulted."""
    broken = _b2b_product_short("p1", "A", 1000)
    del broken["min_price"]  # required by ProductPublicShortResponse

    with patch.object(b2b_client, "_request", AsyncMock(return_value=_b2b_page([broken]))):
        resp = client.get("/api/v1/catalog/products")

    assert resp.status_code == 502
    assert "min_price" in resp.json()["message"]


def test_b2b_invalid_status_returns_502(client: TestClient):
    """`status` outside the B2B ProductStatus enum is a protocol violation."""
    broken = _b2b_product_short("p1", "A", 1000)
    broken["status"] = "PUBLISHED"

    with patch.object(b2b_client, "_request", AsyncMock(return_value=_b2b_page([broken]))):
        resp = client.get("/api/v1/catalog/products")

    assert resp.status_code == 502
    assert "ProductStatus" in resp.json()["message"]


# ======================================================================
# TEST 5 — error format (contract-wide {code, message})
# ======================================================================
def test_error_responses_use_code_message(client: TestClient):
    """Every 4xx body is the flat Error object from the B2C spec."""
    responses = [
        client.get("/api/v1/catalog/products", params={"sort": "nope"}),
        client.get("/api/v1/catalog/products", params={"filter[in_stock]": "maybe"}),
        client.get("/api/v1/catalog/products", params={"limit": 101}),
        client.get("/api/v1/catalog/products", params={"q": "ab"}),
    ]
    for resp in responses:
        assert 400 <= resp.status_code < 500, resp.status_code
        body = resp.json()
        assert set(body) <= {"code", "message", "details"}, body
        assert isinstance(body["code"], str)
        assert isinstance(body["message"], str)
        assert "detail" not in body


def test_short_search_query_returns_400(client: TestClient):
    """q shorter than the B2B minLength(3) → 400 SHORT_SEARCH_QUERY."""
    resp = client.get("/api/v1/catalog/products", params={"q": "ab"})

    assert resp.status_code == 400
    assert resp.json()["code"] == "SHORT_SEARCH_QUERY"


def test_search_query_passes_through_unescaped(client: TestClient):
    """The query is forwarded as typed — B2C does not rewrite the user's input."""
    with fake_b2b() as mock_b2b:
        mock_b2b.list_public_products = AsyncMock(return_value=_parse([]))

        resp = client.get(
            "/api/v1/catalog/products",
            params={"q": "iPhone%15' and 1=1"},
        )

    assert resp.status_code == 200
    assert mock_b2b.list_public_products.call_args.kwargs["search"] == "iPhone%15' and 1=1"


# ======================================================================
# TEST 6 — product card (US-CAT-03) against the real B2B schema
# ======================================================================
def test_product_detail_maps_public_response(client: TestClient):
    """Card is built from ProductPublicResponse / SKUPublicResponse field names."""
    product = _b2b_product(
        "p1",
        "Wireless Headphones",
        skus=[
            _b2b_sku("s1-black", "p1", 4999, active_quantity=10),
            _b2b_sku("s2-white", "p1", 5299, active_quantity=0),
        ],
    )

    with fake_b2b() as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=_detail(product))

        resp = client.get("/api/v1/catalog/products/p1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "p1"
    assert data["name"] == "Wireless Headphones"
    assert data["description"] == "Описание Wireless Headphones"
    assert data["has_stock"] is True
    assert data["min_price"] == 4999
    assert len(data["images"]) == 2
    assert data["images"][0]["is_main"] is True
    assert data["attributes"]["Бренд"] == "Acme"

    assert len(data["skus"]) == 2
    sku = next(s for s in data["skus"] if s["id"] == "s1-black")
    assert sku["available_quantity"] == 10   # from active_quantity
    assert sku["price"] == 4999
    assert sku["sku_code"] == "ART-1"        # from article

    out_of_stock = next(s for s in data["skus"] if s["id"] == "s2-white")
    assert out_of_stock["available_quantity"] == 0


def test_product_detail_min_price_uses_discounted_price(client: TestClient):
    """min_price is the lowest buyer-facing price: price − discount."""
    product = _b2b_product(
        "p1",
        "Discounted",
        skus=[_b2b_sku("s1", "p1", 5000, discount=1000)],
    )

    with fake_b2b() as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=_detail(product))

        resp = client.get("/api/v1/catalog/products/p1")

    assert resp.json()["min_price"] == 4000


def _detail(product: dict):
    from src.services.b2b_public_catalog import parse_product

    return parse_product(product)


def test_product_detail_never_leaks_seller_fields(client: TestClient):
    """Even if B2B adds internal fields, the B2C card exposes only its own schema."""
    product = _b2b_product("p1", "Leaky", skus=[_b2b_sku("s1", "p1", 1000)])
    # Simulate a B2B implementation leaking seller internals.
    product["cost_price"] = 250
    product["skus"][0]["cost_price"] = 250
    product["skus"][0]["reserved_quantity"] = 3

    with fake_b2b() as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=_detail(product))

        resp = client.get("/api/v1/catalog/products/p1")

    assert resp.status_code == 200
    body = resp.json()
    assert "cost_price" not in body
    assert "reserved_quantity" not in json.dumps(body)
    assert "cost_price" not in body["skus"][0]


def test_product_not_found_returns_404(client: TestClient):
    """B2B 404 → B2C 404 PRODUCT_NOT_FOUND."""
    with fake_b2b() as mock_b2b:
        mock_b2b.get_public_product = AsyncMock(return_value=None)

        resp = client.get("/api/v1/catalog/products/nonexistent")

    assert resp.status_code == 404
    assert resp.json()["code"] == "PRODUCT_NOT_FOUND"


# ======================================================================
# TEST 7 — the B2B client speaks the published contract
# ======================================================================
def test_list_public_products_serialises_spec_params(monkeypatch: pytest.MonkeyPatch):
    """Outgoing query params use the B2B names — no invented sort_by/sort_order."""
    import httpx

    from src.services.b2b_client import B2BClient
    from src.services.b2b_public_catalog import parse_product_page

    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_b2b_page([_b2b_product_short("p1", "A", 1000)]))

    monkeypatch.setattr(httpx.AsyncClient, "request", _request_factory(_handler))
    monkeypatch.setattr(
        B2BClient,
        "list_public_products",
        B2BClient.list_public_products,
    )

    client = B2BClient()
    page = asyncio.run(
        client.list_public_products(
            category_id="cat-1",
            search="phone",
            min_price=100,
            max_price=900,
            seller_id="seller-1",
            filters={"brand": "apple", "color": ["red", "blue"]},
            sort="popular",
            limit=20,
            offset=40,
        )
    )

    url = captured["url"]
    assert url.endswith("/public/products") or "/public/products?" in url
    assert "sort=popular" in url
    assert "category_id=cat-1" in url
    assert "search=phone" in url
    assert "min_price=100" in url
    assert "max_price=900" in url
    assert "seller_id=seller-1" in url
    assert "limit=20" in url and "offset=40" in url
    assert "filters%5Bbrand%5D=apple" in url or "filters[brand]=apple" in url
    assert "filters%5Bcolor%5D=red" in url or "filters[color]=red" in url
    # names that do not exist in the B2B schema
    assert "sort_by" not in url
    assert "sort_order" not in url
    assert captured["headers"]["x-service-key"]

    assert page.total_count == 1
    assert page.items[0].title == "A"


def _request_factory(handler):
    """Build a stub for httpx.AsyncClient.request that runs `handler`."""
    from src.services.b2b_client import httpx as b2b_httpx

    async def _request(self, method, url, **kwargs):
        return handler(b2b_httpx.Request(method, url, **{
            k: v for k, v in kwargs.items() if k in ("params", "headers")
        }))

    return _request


def test_client_transport_errors_become_b2b_client_error():
    """Connect errors and timeouts surface as B2BClientError, never as httpx errors."""
    import httpx

    from src.services.b2b_client import B2BClient, B2BClientError

    async def _timeout(self, method, url, **kwargs):
        raise httpx.ReadTimeout("too slow")

    async def _connect(self, method, url, **kwargs):
        raise httpx.ConnectError("connection refused")

    client = B2BClient()

    original_request = httpx.AsyncClient.request
    try:
        httpx.AsyncClient.request = _timeout
        with pytest.raises(B2BClientError) as timeout_info:
            asyncio.run(client.list_public_products())
        assert timeout_info.value.kind == "timeout"

        httpx.AsyncClient.request = _connect
        with pytest.raises(B2BClientError) as connect_info:
            asyncio.run(client.list_public_products())
        assert connect_info.value.kind == "connect"
    finally:
        httpx.AsyncClient.request = original_request


def test_client_rejects_sort_outside_b2b_enum():
    """Guard rail: the client refuses a sort value B2B would reject with 400."""
    from src.services.b2b_client import B2BClient

    with pytest.raises(ValueError):
        asyncio.run(B2BClient().list_public_products(sort="popularity"))


def test_x_service_key_header():
    """Inter-service auth header per the B2B spec."""
    from src.services.b2b_client import B2BClient

    headers = B2BClient()._headers()
    assert headers["X-Service-Key"]