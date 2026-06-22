"""Catalog routes — product listing with filters, sorting, facets, and pagination."""

import re
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List

from src.database import get_db
from src.schemas import (
    ProductDetail,
    CategoryNode,
    FilterOption,
    FilterValue,
    ProductFilters,
    FacetBucket,
    FacetsResponse,
    ProductDetailSchema,
    PaginatedCatalogProducts,
)
from src.services.b2b_client import b2b_client, B2BClientError

router = APIRouter()

# ---------------------------------------------------------------------------
# Search validation
# ---------------------------------------------------------------------------
SEARCH_MIN_LENGTH: int = 3

# SQL metacharacters that must be escaped so the query doesn't break
_SEARCH_ESCAPE_RE = re.compile(r"([%_'])")


def _validate_search(query: Optional[str]) -> Optional[str]:
    """Validate and sanitise the search query.

    Raises 400 when the query is shorter than the minimum length.
    Escapes SQL metacharacters (% _ ') so the downstream B2B layer
    receives a safe literal string.
    """
    if query is None or query.strip() == "":
        return None

    if len(query) < SEARCH_MIN_LENGTH:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SHORT_SEARCH_QUERY",
                "message": f"Search query must be at least {SEARCH_MIN_LENGTH} characters",
            },
        )

    # Escape SQL metacharacters
    return _SEARCH_ESCAPE_RE.sub(r"\\\1", query)


# ---------------------------------------------------------------------------
# Allowed sort values
# ---------------------------------------------------------------------------
ALLOWED_SORT_VALUES: list[str] = [
    "price_asc",
    "price_desc",
    "popularity",
    "new",
]

# Mapping from sort enum to B2B sort_by + sort_order
SORT_MAP: dict[str, tuple[str, str]] = {
    "price_asc": ("price", "asc"),
    "price_desc": ("price", "desc"),
    "popularity": ("popularity", "desc"),
    "new": ("created_at", "desc"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_facets_from_b2b(
    category_id: Optional[str] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    brand: Optional[str] = None,
    in_stock: Optional[bool] = None,
    search: Optional[str] = None,
) -> dict:
    """Common helper: call B2B facets and raise 502 on error."""
    try:
        result = await b2b_client.get_facets(
            category_id=category_id,
            search=search,
            min_price=price_min,
            max_price=price_max,
            in_stock=in_stock,
            facet_fields=["brand", "price_range", "rating", "in_stock"],
        )
        return result
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)


def _normalise_facets(raw_facets: dict) -> List[FacetBucket]:
    """Normalise B2B facets dict/list into FacetBucket list."""
    facets: List[FacetBucket] = []
    for field_name, buckets in raw_facets.items():
        if isinstance(buckets, list):
            facet_values = [
                {"value": b.get("value", ""), "label": b.get("label", b.get("value", "")), "count": b.get("count", 0)}
                for b in buckets
            ]
        elif isinstance(buckets, dict):
            facet_values = [
                {"value": k, "label": k, "count": v} for k, v in buckets.items()
            ]
        else:
            facet_values = []

        facets.append(FacetBucket(name=field_name, label=field_name.replace("_", " ").title(), values=facet_values))
    return facets
def _map_b2b_product(raw: dict) -> dict:
    """Normalise a B2B product dict into ProductDetail shape."""
    return {
        "id": str(raw.get("product_id", "")),
        "name": raw.get("title", ""),
        "main_image_url": raw.get("main_image_url", ""),
        "min_price": float(raw.get("min_price", 0.0)),
        "has_stock": bool(raw.get("is_available", True)),
        "description": raw.get("description", ""),
        "images": raw.get("images", []),
        "characteristics": raw.get("characteristics", {}),
        "skus": _map_b2b_skus(raw.get("skus", [])),
    }


def _map_b2b_skus(raw_skus: list[dict]) -> list[dict]:
    """Map B2B SKU dict → B2C-safe SKU dict, stripping sensitive fields.

    CRITICAL: cost_price, reserved_quantity and any other internal seller
    fields MUST NOT appear in the response.  This is a security boundary.

    Also renames B2B fields to B2C OpenAPI names:
      sku_id → id, quantity_available → available_quantity
    """
    excluded_keys = {"cost_price", "reserved_quantity"}
    skus = []
    for raw in raw_skus:
        # Strip excluded first
        filtered = {k: v for k, v in raw.items() if k not in excluded_keys}
        sku = {
            "id": filtered.pop("sku_id", ""),
            "color": filtered.pop("color", None),
            "size": filtered.pop("size", None),
            "other_specs": filtered.pop("other_specs", None),
            "price": filtered.pop("price", 0.0),
            "available_quantity": filtered.pop("quantity_available", 0),
            "is_active": filtered.pop("is_active", True),
            "discount": filtered.pop("discount", 0.0),
        }
        sku["in_stock"] = bool(sku["available_quantity"] > 0)
        skus.append(sku)
    return skus


def _b2b_error(status_code: int, message: str) -> HTTPException:
    """Convert a B2BClientError into an HTTPException with OpenAPI error format."""
    return HTTPException(
        status_code=status_code,
        detail={"code": "B2B_UNAVAILABLE", "message": message},
    )


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/categories
# ---------------------------------------------------------------------------
@router.get("/categories", response_model=list[dict])
async def get_categories():
    """Get the full category tree from B2B."""
    try:
        categories = await b2b_client.get_categories()
        return categories
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/products
# ---------------------------------------------------------------------------
@router.get("/products", response_model=PaginatedCatalogProducts)
async def get_products(
    category_id: Optional[str] = Query(None, alias="filter[category_id]", description="Category ID filter"),
    price_min: Optional[float] = Query(None, alias="filter[price_min]", description="Minimum price"),
    price_max: Optional[float] = Query(None, alias="filter[price_max]", description="Maximum price"),
    brand: Optional[str] = Query(None, alias="filter[brand]", description="Brand slug"),
    in_stock: Optional[str] = Query(None, alias="filter[in_stock]", description="in_stock=true/false"),
    q: Optional[str] = Query(None, description="Search by product name / description"),
    sort: Optional[str] = Query(None, description="Sort: price_asc, price_desc, popularity, new"),
    limit: int = Query(20, ge=1, le=100, description="Items per page (1–100)"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    db: AsyncSession = Depends(get_db),
):
    """Get products with filtering, sorting, and pagination."""
    # --- Parse in_stock ---
    in_stock_bool: Optional[bool] = None
    if in_stock is not None:
        in_stock_bool = in_stock.lower() == "true"

    # --- Validate and sanitise search query ---
    safe_search = _validate_search(q)

    # --- Validate sort ---
    sort_by = "popularity"
    sort_order = "desc"
    if sort is not None:
        if sort not in ALLOWED_SORT_VALUES:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_SORT",
                    "message": f"Invalid sort value '{sort}'. Allowed values: {ALLOWED_SORT_VALUES}",
                },
            )
        sort_by, sort_order = SORT_MAP[sort]

    # --- Call B2B ---
    try:
        result = await b2b_client.get_products(
            category_id=category_id,
            search=safe_search,
            min_price=price_min,
            max_price=price_max,
            in_stock=in_stock_bool,
            brand=brand,
            sort_by=sort_by,
            sort_order=sort_order,
            page=offset // limit + 1 if limit > 0 else 1,
            page_size=limit,
        )
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)

    # --- Map response ---
    products = [_map_b2b_product(p) for p in result.get("products", [])]

    product_details = []
    for p in products:
        product_details.append(
            ProductDetailSchema(
                id=p["id"],
                name=p["name"],
                main_image_url=p["main_image_url"],
                min_price=p["min_price"],
                has_stock=p["has_stock"],
                description=p["description"],
                images=p["images"],
                characteristics=p["characteristics"],
                skus=p["skus"],
            )
        )

    return PaginatedCatalogProducts(
        items=product_details,
        total_count=result.get("total", len(product_details)),
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/products/{product_id}
# ---------------------------------------------------------------------------
@router.get("/products/{product_id}", response_model=ProductDetail)
async def get_product(product_id: str, db: AsyncSession = Depends(get_db)):
    """Get full product details."""
    try:
        product_data = await b2b_client.get_product_by_id(product_id)
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)

    if product_data is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )

    product_data = _map_b2b_product(product_data)
    return ProductDetail(
        id=product_data["id"],
        name=product_data["name"],
        main_image_url=product_data["main_image_url"],
        min_price=product_data["min_price"],
        has_stock=product_data["has_stock"],
        description=product_data["description"],
        images=product_data["images"],
        characteristics=product_data["characteristics"],
        skus=product_data["skus"],
    )


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/facets
# ---------------------------------------------------------------------------
@router.get("/facets", response_model=FacetsResponse)
async def get_facets(
    category_id: Optional[str] = Query(None, alias="filter[category_id]", description="Category ID filter"),
    price_min: Optional[float] = Query(None, alias="filter[price_min]", description="Minimum price"),
    price_max: Optional[float] = Query(None, alias="filter[price_max]", description="Maximum price"),
    brand: Optional[str] = Query(None, alias="filter[brand]", description="Brand slug"),
    in_stock: Optional[str] = Query(None, alias="filter[in_stock]", description="in_stock=true/false"),
    q: Optional[str] = Query(None, description="Search facet count by query text"),
    db: AsyncSession = Depends(get_db),
):
    """Get facet counts for the current filter context."""
    in_stock_bool: Optional[bool] = None
    if in_stock is not None:
        in_stock_bool = in_stock.lower() == "true"

    try:
        result = await b2b_client.get_facets(
            category_id=category_id,
            search=q,
            min_price=price_min,
            max_price=price_max,
            in_stock=in_stock_bool,
            facet_fields=["brand", "price_range", "rating", "in_stock"],
        )
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)

    # Normalise the B2B response into FacetBucket list
    raw_facets = result.get("facets", {})
    facets: List[FacetBucket] = []
    for field_name, buckets in raw_facets.items():
        if isinstance(buckets, list):
            facet_values = [
                {"value": b.get("value", ""), "label": b.get("label", b.get("value", "")), "count": b.get("count", 0)}
                for b in buckets
            ]
        elif isinstance(buckets, dict):
            facet_values = [
                {"value": k, "label": k, "count": v} for k, v in buckets.items()
            ]
        else:
            facet_values = []

        facets.append(FacetBucket(name=field_name, label=field_name.replace("_", " ").title(), values=facet_values))

    return FacetsResponse(
        category_id=category_id or "",
        facets=facets,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/categories/{category_id}/filters
# ---------------------------------------------------------------------------
@router.get("/categories/{category_id}/filters", response_model=ProductFilters)
async def get_category_filters(category_id: str, db: AsyncSession = Depends(get_db)):
    """Get available static filters for a category (legacy endpoint)."""
    try:
        result = await _get_facets_from_b2b(category_id=category_id)
        facets = _normalise_facets(result.get("facets", {}))
        values_map: dict[str, List[FilterValue]] = {}
        for fb in facets:
            values_map[fb.name] = fb.values

        filters_list = []
        for name, values in values_map.items():
            filters_list.append(
                FilterOption(name=name, label=name.replace("_", " ").title(), values=values)
            )

        return ProductFilters(category_id=category_id, filters=filters_list if filters_list else [])
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)