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
    RecommendationList,
    ProductBasic,
    CategoryDetail,
    BreadcrumbItem,
    BreadcrumbsResponse,
    CategoryItem,
    CategoryListResponse,
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
def _as_product_list(result) -> list:
    """Normalise B2B list responses: plain array or paginated envelope."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("items", [])
    return []


def _map_b2b_characteristics(raw_chars) -> dict:
    """Map B2B characteristics [{name, value}] into a plain dict."""
    if isinstance(raw_chars, dict):
        return {str(k): str(v) for k, v in raw_chars.items()}
    result: dict = {}
    for item in raw_chars or []:
        if isinstance(item, dict) and "name" in item:
            result[str(item["name"])] = str(item.get("value", ""))
    return result


def _map_b2b_product(raw: dict) -> dict:
    """Normalise a B2B product dict into ProductDetail shape.

    Accepts both ProductPublicShortResponse (list) and ProductPublicResponse
    (card): id/title/cover_image/min_price per b2b/openapi.yaml.
    """
    cover = raw.get("cover_image") or ""
    images_raw = raw.get("images", [])
    if images_raw and isinstance(images_raw[0], dict):
        image_urls = [i.get("url", "") for i in images_raw if isinstance(i, dict)]
    else:
        image_urls = [str(i) for i in images_raw if i]
    main_image = cover or (image_urls[0] if image_urls else "")

    skus_raw = raw.get("skus", [])
    has_stock = any(
        (s.get("active_quantity", 0) if isinstance(s, dict) else 0) > 0
        for s in skus_raw
    ) if skus_raw else bool(raw.get("has_stock", True))

    return {
        "id": str(raw.get("id", "")),
        "name": raw.get("title", ""),
        "main_image_url": main_image,
        "min_price": float(raw.get("min_price", 0.0)),
        "has_stock": has_stock,
        "description": raw.get("description", ""),
        "images": image_urls,
        "characteristics": _map_b2b_characteristics(raw.get("characteristics", [])),
        "skus": _map_b2b_skus(skus_raw),
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
        available = int(filtered.pop("active_quantity", filtered.pop("quantity_available", 0)) or 0)
        sku = {
            "id": str(filtered.pop("id", filtered.pop("sku_id", ""))),
            "color": filtered.pop("color", None),
            "size": filtered.pop("size", None),
            "other_specs": filtered.pop("other_specs", None),
            "price": float(filtered.pop("price", 0.0)),
            "available_quantity": available,
            "is_active": bool(filtered.pop("is_active", True)),
            "discount": float(filtered.pop("discount", 0.0)),
        }
        sku["in_stock"] = available > 0
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
@router.get("/categories", response_model=CategoryListResponse)
async def get_categories():
    """Get the full category tree from B2B."""
    try:
        categories = await b2b_client.get_categories()
        items = [
            CategoryItem(
                category_id=str(c.get("id", c.get("category_id", ""))),
                name=str(c.get("name", "")),
                parent_id=c.get("parent_id"),
            )
            for c in categories
        ]
        return CategoryListResponse(items=items)
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
            limit=limit,
            offset=offset,
        )
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)

    # --- Map response (B2B contract: items/total_count) ---
    products = [_map_b2b_product(p) for p in result.get("items", [])]

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
        total_count=result.get("total_count", len(product_details)),
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
# GET /api/v1/catalog/products/{product_id}/similar
# ---------------------------------------------------------------------------
SIMILAR_LIMIT: int = 8


@router.get("/products/{product_id}/similar", response_model=RecommendationList)
async def get_similar_products(
    product_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get similar products from the same category, excluding the current product.

    Algorithm (canon-flow):
      1. Fetch current product to get its category_id and parent_category_id.
      2. Query B2B for similar products in the same category (up to 8).
      3. If fewer than 8, fill from parent category.
      4. Always exclude the current product from results.
      5. If category has no products → return 200 with empty list.
      6. If product not found → return 404.
    """
    # Step 1: get current product to know its category
    try:
        current_product = await b2b_client.get_product_by_id(product_id)
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)

    if current_product is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )

    category_id: str = current_product.get("category_id", "")
    parent_category_id: Optional[str] = current_product.get("parent_category_id")

    # Step 2: get similar from same category
    similar_ids: set = set()
    recommendations: List[ProductBasic] = []
    from_same_category: bool = False

    if category_id:
        try:
            similar_result = await b2b_client.get_similar_products(
                product_id=product_id,
                category_id=category_id,
                limit=SIMILAR_LIMIT,
            )
        except B2BClientError as exc:
            raise _b2b_error(502, exc.message)

        for raw in _as_product_list(similar_result):
            pid = str(raw.get("id", ""))
            if pid and pid != product_id and pid not in similar_ids:
                similar_ids.add(pid)
                recommendations.append(
                    ProductBasic(
                        id=pid,
                        name=raw.get("title", ""),
                        main_image_url=raw.get("cover_image") or "",
                        min_price=float(raw.get("min_price", 0.0)),
                        has_stock=True,
                    )
                )
                if len(recommendations) >= SIMILAR_LIMIT:
                    break

        from_same_category = len(recommendations) > 0

    # Step 3: fallback to parent category if not enough
    filled_from_parent = False
    if len(recommendations) < SIMILAR_LIMIT and parent_category_id:
        try:
            more_result = await b2b_client.get_similar_products(
                product_id=product_id,
                category_id=parent_category_id,
                limit=(SIMILAR_LIMIT - len(recommendations)) * 2,
            )
        except B2BClientError as exc:
            # Non-fatal — log and continue with what we have
            pass
        else:
            for raw in _as_product_list(more_result):
                pid = str(raw.get("id", ""))
                if pid and pid != product_id and pid not in similar_ids:
                    similar_ids.add(pid)
                    recommendations.append(
                        ProductBasic(
                            id=pid,
                            name=raw.get("title", ""),
                            main_image_url=raw.get("cover_image") or "",
                            min_price=float(raw.get("min_price", 0.0)),
                            has_stock=True,
                        )
                    )
                    if len(recommendations) >= SIMILAR_LIMIT:
                        break
            filled_from_parent = len(_as_product_list(more_result)) > 0

    # Determine reason
    if not recommendations:
        reason = "no_similar_products"
    elif filled_from_parent:
        reason = "parent_category"
    elif from_same_category:
        reason = "same_category"
    else:
        reason = "no_similar_products"

    return RecommendationList(
        current_product_id=product_id,
        recommendations=recommendations,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Helpers for category navigation
# ---------------------------------------------------------------------------

def _build_category_tree(flat_categories: List[dict]) -> List[CategoryNode]:
    """Build a nested tree from a flat list of category dicts.

    Each dict must have at least `category_id`, `name`, and optionally `parent_id`.
    """
    by_id: dict[str, CategoryNode] = {}
    roots: List[CategoryNode] = []

    for c in flat_categories:
        cid = str(c.get("id", c.get("category_id", "")))
        if not cid:
            continue
        by_id[cid] = CategoryNode(
            category_id=cid,
            name=str(c.get("name", "")),
            parent_id=c.get("parent_id"),
        )

    for cid, node in by_id.items():
        if node.parent_id and node.parent_id in by_id:
            by_id[node.parent_id].children.append(node)
        else:
            roots.append(node)

    return roots


def _find_category(
    category_id: str,
    tree: List[CategoryNode],
) -> Optional[CategoryNode]:
    """Find a category node by ID in a nested tree."""
    for node in tree:
        if node.category_id == category_id:
            return node
        found = _find_category(category_id, node.children)
        if found:
            return found
    return None


def _build_breadcrumbs(
    category_id: str,
    flat_categories: List[dict],
) -> List[BreadcrumbItem]:
    """Build breadcrumbs from root to target category.

    Raises ValueError on orphan node (parent_id points to non-existent category).
    """
    by_id: dict[str, dict] = {}
    for c in flat_categories:
        cid = str(c.get("id", c.get("category_id", "")))
        if cid:
            by_id[cid] = c

    if category_id not in by_id:
        raise ValueError(f"Category {category_id} not found")

    path: List[dict] = []
    current_id: Optional[str] = category_id
    visited: set = set()

    while current_id:
        if current_id in visited:
            # Cycle detected — treat as orphan
            raise ValueError(f"Cycle detected in category hierarchy at {current_id}")
        visited.add(current_id)

        if current_id not in by_id:
            raise ValueError(f"Orphan node: {current_id} has no definition in category list")

        entry = by_id[current_id]
        path.append(entry)
        current_id = entry.get("parent_id")

    # Reverse so root → leaf
    path.reverse()
    return [
        BreadcrumbItem(
            category_id=str(p.get("id", p.get("category_id", ""))),
            name=str(p.get("name", "")),
            parent_id=p.get("parent_id"),
        )
        for p in path
    ]


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/categories/tree
# ---------------------------------------------------------------------------

@router.get("/categories/tree", response_model=list[CategoryNode])
async def get_category_tree():
    """Get the full category tree as a nested structure."""
    try:
        flat = await b2b_client.get_categories()
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)

    tree = _build_category_tree(flat)
    return tree


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/categories/{category_id}
# ---------------------------------------------------------------------------

@router.get("/categories/{category_id}", response_model=CategoryDetail)
async def get_category_detail(category_id: str):
    """Get details for a single category."""
    try:
        flat = await b2b_client.get_categories()
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)

    node = _find_category(category_id, _build_category_tree(flat))
    if node is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "CATEGORY_NOT_FOUND", "message": f"Category {category_id} not found"},
        )

    return CategoryDetail(
        category_id=node.category_id,
        name=node.name,
        parent_id=node.parent_id,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/breadcrumbs
# ---------------------------------------------------------------------------

@router.get("/breadcrumbs", response_model=BreadcrumbsResponse)
async def get_breadcrumbs(
    category_id: Optional[str] = Query(None, description="Category ID for breadcrumbs"),
    product_id: Optional[str] = Query(None, description="Product ID — breadcrumbs built from its category"),
):
    """Build breadcrumbs path from root to the target category.

    Accepts exactly one of: category_id or product_id.
    Both provided → 400. Neither provided → 400.
    Orphan / broken hierarchy → 422.
    Unknown category → 404.
    """
    if category_id and product_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "AMBIGUOUS_PARAMS",
                "message": "Provide exactly one of: category_id or product_id",
            },
        )

    if not category_id and not product_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MISSING_PARAMS",
                "message": "Provide either category_id or product_id",
            },
        )

    target_category_id: Optional[str] = category_id

    # If only product_id given, resolve its category
    if product_id and not category_id:
        try:
            product = await b2b_client.get_product_by_id(product_id)
        except B2BClientError as exc:
            raise _b2b_error(502, exc.message)

        if product is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
            )

        target_category_id = product.get("category_id")
        if not target_category_id:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "CATEGORY_MISSING",
                    "message": "Product has no category_id",
                },
            )

    if not target_category_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "NO_TARGET", "message": "No category resolved"},
        )

    try:
        flat = await b2b_client.get_categories()
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)

    try:
        breadcrumbs = _build_breadcrumbs(target_category_id, flat)
    except ValueError as exc:
        msg = str(exc)
        if "Orphan" in msg or "Cycle" in msg:
            raise HTTPException(
                status_code=422,
                detail={"code": "ORPHAN_NODE", "message": msg},
            )
        # "not found"
        raise HTTPException(
            status_code=404,
            detail={"code": "CATEGORY_NOT_FOUND", "message": msg},
        )

    return BreadcrumbsResponse(items=breadcrumbs)


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