"""Catalog routes — product listing with filters, sorting, facets, and pagination."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List
from pydantic import BaseModel, Field

from src.database import get_db
from src.schemas import (
    ProductDetail,
    CategoryNode,
    FilterOption,
    FilterValue,
    ProductFilters,
    FacetBucket,
    FacetsResponse,
    ProductListResponse,
)
from src.services.b2b_client import b2b_client, B2BClientError

router = APIRouter()

# ---------------------------------------------------------------------------
# Allowed sort parameters
# ---------------------------------------------------------------------------
ALLOWED_SORT_FIELDS: list[str] = [
    "price",
    "title",
    "created_at",
    "rating",
    "popularity",
]
ALLOWED_SORT_ORDERS: list[str] = ["asc", "desc"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _map_b2b_product(raw: dict) -> dict:
    """Normalise a B2B product dict into ProductDetail shape."""
    return {
        "product_id": str(raw.get("product_id", "")),
        "title": raw.get("title", ""),
        "main_image_url": raw.get("main_image_url", ""),
        "min_price": float(raw.get("min_price", 0.0)),
        "is_available": bool(raw.get("is_available", True)),
        "description": raw.get("description", ""),
        "images": raw.get("images", []),
        "characteristics": raw.get("characteristics", {}),
        "skus": raw.get("skus", []),
    }


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
        raise HTTPException(
            status_code=502,
            detail={"error": "B2B_UNAVAILABLE", "message": exc.message, "status_code": exc.status_code},
        )


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/products
# ---------------------------------------------------------------------------
@router.get("/products", response_model=ProductListResponse)
async def get_products(
    category_id: Optional[str] = Query(None, description="Filter by category ID"),
    search: Optional[str] = Query(None, description="Search by product name / description"),
    min_price: Optional[float] = Query(None, description="Minimum price (inclusive)"),
    max_price: Optional[float] = Query(None, description="Maximum price (inclusive)"),
    in_stock: Optional[bool] = Query(None, description="Only products with active_quantity > 0"),
    brand: Optional[str] = Query(None, description="Filter by brand slug"),
    sort_by: Optional[str] = Query("popularity", description="Sort field"),
    sort_order: Optional[str] = Query("desc", description="Sort order: asc | desc"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page (1–100)"),
    db: AsyncSession = Depends(get_db),
):
    """Get products with filtering, sorting, and pagination.

    Visibility: only status = MODERATED AND deleted = false AND active_quantity > 0.
    """
    # --- Validate sort_by ---------------------------------------------------
    if sort_by not in ALLOWED_SORT_FIELDS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_SORT_FIELD",
                "message": f"Invalid sort_by value '{sort_by}'. Allowed values: {ALLOWED_SORT_FIELDS}",
                "allowed": ALLOWED_SORT_FIELDS,
            },
        )

    # --- Validate sort_order ------------------------------------------------
    if sort_order not in ALLOWED_SORT_ORDERS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INVALID_SORT_ORDER",
                "message": f"Invalid sort_order value '{sort_order}'. Allowed values: {ALLOWED_SORT_ORDERS}",
                "allowed": ALLOWED_SORT_ORDERS,
            },
        )

    # --- Build filter dict --------------------------------------------------
    filters: dict = {}
    if category_id:
        filters["category_id"] = category_id
    if search:
        filters["search"] = search
    if min_price is not None:
        filters["min_price"] = min_price
    if max_price is not None:
        filters["max_price"] = max_price
    if in_stock is not None:
        filters["in_stock"] = in_stock
    if brand:
        filters["brand"] = brand

    # --- Call B2B -----------------------------------------------------------
    try:
        result = await b2b_client.get_products(
            category_id=category_id,
            search=search,
            min_price=min_price,
            max_price=max_price,
            in_stock=in_stock,
            brand=brand,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )
    except B2BClientError as exc:
        # B2B unavailable → 502 Bad Gateway
        raise HTTPException(
            status_code=502,
            detail={
                "error": "B2B_UNAVAILABLE",
                "message": exc.message,
                "status_code": exc.status_code,
            },
        )

    # --- Map response -------------------------------------------------------
    products = [_map_b2b_product(p) for p in result.get("products", [])]

    # Wrap each product as ProductDetail
    product_details = []
    for p in products:
        product_details.append(
            ProductDetail(
                product_id=p["product_id"],
                title=p["title"],
                main_image_url=p["main_image_url"],
                min_price=p["min_price"],
                is_available=p["is_available"],
                description=p["description"],
                images=p["images"],
                characteristics=p["characteristics"],
                skus=p["skus"],
            )
        )

    return ProductListResponse(
        products=product_details,
        total=result.get("total", len(products)),
        page=page,
        page_size=page_size,
        filters_applied=filters if filters else None,
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
        raise HTTPException(
            status_code=502,
            detail={"error": "B2B_UNAVAILABLE", "message": exc.message},
        )

    if not product_data:
        raise HTTPException(status_code=404, detail={"error": "PRODUCT_NOT_FOUND", "message": "Product not found"})

    return ProductDetail(
        product_id=product_data.get("product_id", ""),
        title=product_data.get("title", ""),
        main_image_url=product_data.get("main_image_url", ""),
        min_price=product_data.get("min_price", 0.0),
        is_available=product_data.get("is_available", True),
        description=product_data.get("description", ""),
        images=product_data.get("images", []),
        characteristics=product_data.get("characteristics", {}),
        skus=product_data.get("skus", []),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/facets
# ---------------------------------------------------------------------------
@router.get("/facets", response_model=FacetsResponse)
async def get_facets(
    category_id: Optional[str] = Query(None, description="Filter facets by category"),
    search: Optional[str] = Query(None, description="Search facet count by query text"),
    min_price: Optional[float] = Query(None, description="Minimum price for facet calculation"),
    max_price: Optional[float] = Query(None, description="Maximum price for facet calculation"),
    in_stock: Optional[bool] = Query(None, description="Only count in-stock items"),
    brand: Optional[str] = Query(None, description="Filter facets by brand"),
    db: AsyncSession = Depends(get_db),
):
    """Get facet counts for the current filter context.

    Returns the number of matching products for each filter value
    (brand, price_range, etc.) so the UI can display live counts.
    """
    params: dict = {}
    if category_id:
        params["category_id"] = category_id
    if search:
        params["search"] = search
    if min_price is not None:
        params["min_price"] = min_price
    if max_price is not None:
        params["max_price"] = max_price
    if in_stock is not None:
        params["in_stock"] = in_stock
    if brand:
        params["brand"] = brand

    try:
        result = await b2b_client.get_facets(
            category_id=category_id,
            search=search,
            min_price=min_price,
            max_price=max_price,
            in_stock=in_stock,
            facet_fields=["brand", "price_range", "rating", "in_stock"],
        )
    except B2BClientError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "B2B_UNAVAILABLE", "message": exc.message},
        )

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
    """Get available static filters for a category (legacy endpoint).

    Prefers live data from B2B; falls back to static defaults.
    """
    try:
        facets = await get_facets(category_id=category_id)
        # Convert FacetBucket → FilterOption
        values_map: dict[str, List[FilterValue]] = {}
        for fb in facets.facets:
            values_map[fb.name] = fb.values

        filters_list = []
        for name, values in values_map.items():
            filters_list.append(
                FilterOption(name=name, label=name.replace("_", " ").title(), values=values)
            )

        return ProductFilters(category_id=category_id, filters=filters_list if filters_list else [])
    except B2BClientError as exc:
        raise HTTPException(status_code=502, detail={"error": "B2B_UNAVAILABLE", "message": exc.message})