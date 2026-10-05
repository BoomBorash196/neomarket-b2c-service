"""Catalog routes — product listing with filters, sorting, facets, and pagination.

B2B integration rule: the storefront catalogue lives in B2B. Every request below
is built from the published B2B contract (``b2b/openapi.yaml``, tag
``Public Catalog``) and every response is read through
``src.services.b2b_public_catalog``. Visibility (MODERATED, not deleted,
active_quantity > 0) is applied by B2B — B2C never filters on those fields.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.schemas import (
    BreadcrumbItem,
    BreadcrumbsResponse,
    CatalogProductCard,
    CatalogProductDetail,
    CatalogSku,
    CategoryDetail,
    CategoryItem,
    CategoryListResponse,
    CategoryNode,
    FacetBucket,
    FacetsResponse,
    FilterOption,
    FilterValue,
    ImageRef,
    PaginatedCatalogProducts,
    ProductBasic,
    ProductFilters,
    RecommendationList,
)
from src.services.b2b_client import b2b_client, B2BClientError
from src.services.b2b_public_catalog import B2BProduct, B2BProductShort
from src.services.facet_service import FacetSelection, get_facets as compute_facets

router = APIRouter()

# ---------------------------------------------------------------------------
# Search validation
# ---------------------------------------------------------------------------
SEARCH_MIN_LENGTH: int = 3
SEARCH_MAX_LENGTH: int = 200


def _validate_search(query: Optional[str]) -> Optional[str]:
    """Validate the `q` parameter.

    Length rules mirror the B2B `search` param (minLength 3) and the B2C `q`
    param (maxLength 200). The value itself is passed through untouched: it is a
    query-string parameter that the receiving service is responsible for
    encoding/escaping, and pre-escaping here corrupts the user's input.
    """
    if query is None or query.strip() == "":
        return None

    stripped = query.strip()
    if len(stripped) < SEARCH_MIN_LENGTH:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SHORT_SEARCH_QUERY",
                "message": f"Search query must be at least {SEARCH_MIN_LENGTH} characters",
            },
        )
    if len(stripped) > SEARCH_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SEARCH_QUERY_TOO_LONG",
                "message": f"Search query must be at most {SEARCH_MAX_LENGTH} characters",
            },
        )
    return stripped


# ---------------------------------------------------------------------------
# Allowed sort values (B2C surface → B2B sort enum)
# ---------------------------------------------------------------------------
ALLOWED_SORT_VALUES: list[str] = [
    "price_asc",
    "price_desc",
    "popularity",
    "new",
]

# B2C exposes popularity/new; B2B's enum spells them `popular` / `created_desc`.
SORT_MAP: dict[str, str] = {
    "price_asc": "price_asc",
    "price_desc": "price_desc",
    "popularity": "popular",
    "new": "created_desc",
}

DEFAULT_SORT: str = "popularity"


def _b2b_sort(sort: Optional[str]) -> str:
    """Validate a B2C sort value and translate it to the B2B sort enum."""
    if sort is None:
        return SORT_MAP[DEFAULT_SORT]
    if sort not in ALLOWED_SORT_VALUES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_SORT",
                "message": f"Invalid sort value '{sort}'. Allowed values: {ALLOWED_SORT_VALUES}",
            },
        )
    return SORT_MAP[sort]


def _parse_in_stock(raw: Optional[str]) -> Optional[bool]:
    """`filter[in_stock]` → bool. Unknown values are rejected rather than ignored."""
    if raw is None or raw == "":
        return None
    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise HTTPException(
        status_code=400,
        detail={
            "code": "INVALID_FILTER_VALUE",
            "message": f"Invalid filter[in_stock] value '{raw}'. Allowed values: true, false",
        },
    )


def _b2b_filters(brand: Optional[str], attributes: Optional[dict]) -> Optional[dict]:
    """Build B2B dynamic `filters[...]` params from B2C filter inputs.

    B2B declares `filters` as a deepObject keyed by characteristic name
    (snake_case), e.g. `?filters[brand]=apple`. B2C's `filter[brand]` and
    `filter[attributes][<key>]` both map onto it.
    """
    collected: dict[str, object] = {}
    if brand:
        collected["brand"] = brand
    for key, value in (attributes or {}).items():
        collected[key] = value
    return collected or None


def _sorted_attribute_items(filters: Optional[dict]) -> tuple[tuple[str, str], ...]:
    """Deterministically ordered (key, value) pairs — the facet cache key."""
    if not filters:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in filters.items()))


def _extract_attributes(request: Request) -> dict[str, str]:
    """Read `filter[attributes][<key>]` pairs out of the raw query string."""
    attributes: dict[str, str] = {}
    for raw_key, value in request.query_params.multi_items():
        if raw_key.startswith("filter[attributes][") and raw_key.endswith("]"):
            key = raw_key[len("filter[attributes][") : -1]
            if key:
                attributes[key] = value
    return attributes


# ---------------------------------------------------------------------------
# Mapping: B2B public payloads → B2C response schemas
# ---------------------------------------------------------------------------
def _cover_images(product: B2BProductShort) -> List[ImageRef]:
    """Card images for a listing row.

    ``ProductPublicShortResponse`` carries a single ``cover_image`` URL string
    and no image objects, so the card gets that one image. ``ImageRef.id`` is
    required by the B2C contract and B2B supplies no id for the cover, so the
    product id is used — it is stable and unique within the card.
    """
    if not product.cover_image:
        return []
    return [
        ImageRef(
            id=f"{product.id}-cover",
            url=product.cover_image,
            ordering=0,
            is_main=True,
        )
    ]


def _card_from_short(product: B2BProductShort) -> CatalogProductCard:
    """`ProductPublicShortResponse` → B2C `CatalogProductCard`."""
    return CatalogProductCard(
        id=product.id,
        name=product.title,
        slug=product.slug,
        min_price=product.min_price,
        has_stock=True,  # B2B public listing only returns active_quantity > 0
        images=_cover_images(product),
    )


def _attributes_of(characteristics) -> dict[str, str]:
    return {c.name: c.value for c in characteristics}


def _detail_from_product(product: B2BProduct) -> CatalogProductDetail:
    """`ProductPublicResponse` → B2C `CatalogProductDetail`."""
    skus = [
        CatalogSku(
            id=sku.id,
            name=sku.name,
            sku_code=sku.article,
            price=sku.final_price,
            old_price=sku.price if sku.discount > 0 else None,
            available_quantity=sku.active_quantity,
            attributes=_attributes_of(sku.characteristics),
            images=[
                ImageRef(url=i.url, id=i.id, ordering=i.ordering)
                for i in sorted(sku.images, key=lambda img: img.ordering)
            ],
        )
        for sku in product.skus
    ]
    images = [
        ImageRef(url=i.url, id=i.id, ordering=i.ordering, is_main=i.ordering == 0)
        for i in sorted(product.images, key=lambda img: img.ordering)
    ]
    return CatalogProductDetail(
        id=product.id,
        name=product.title,
        slug=product.slug,
        min_price=product.min_price,
        has_stock=product.in_stock,
        images=images,
        description=product.description,
        attributes=_attributes_of(product.characteristics),
        skus=skus,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _b2b_error(status_code: int, message: str) -> HTTPException:
    """Convert a B2BClientError into an HTTPException with the OpenAPI error format."""
    return HTTPException(
        status_code=status_code,
        detail={"code": "B2B_UNAVAILABLE", "message": message},
    )


async def _b2b_call(coro):
    """Run a B2B call, mapping any upstream failure to 502."""
    try:
        return await coro
    except B2BClientError as exc:
        raise _b2b_error(502, exc.message)


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
                category_id=str(c.get("category_id", "")),
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
    request: Request,
    category_id: Optional[str] = Query(None, alias="filter[category_id]", description="Category ID filter"),
    price_min: Optional[int] = Query(None, alias="filter[price_min]", ge=0, description="Minimum price, kopecks"),
    price_max: Optional[int] = Query(None, alias="filter[price_max]", ge=0, description="Maximum price, kopecks"),
    seller_id: Optional[str] = Query(None, alias="filter[seller_id]", description="Seller ID filter"),
    brand: Optional[str] = Query(None, alias="filter[brand]", description="Brand filter"),
    in_stock: Optional[str] = Query(None, alias="filter[in_stock]", description="in_stock=true/false"),
    q: Optional[str] = Query(None, max_length=SEARCH_MAX_LENGTH, description="Search by product name / description"),
    sort: Optional[str] = Query(None, description=f"Sort: {', '.join(ALLOWED_SORT_VALUES)}"),
    limit: int = Query(20, ge=1, le=100, description="Items per page (1–100)"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    db: AsyncSession = Depends(get_db),
):
    """Get products with filtering, sorting, and pagination.

    B2B call: ``GET /public/products`` with the spec's own parameter names —
    ``category_id``, ``search``, ``min_price``, ``max_price``, ``seller_id``,
    ``filters[...]``, ``sort``, ``limit``, ``offset``.
    """
    in_stock_bool = _parse_in_stock(in_stock)
    safe_search = _validate_search(q)
    b2b_sort = _b2b_sort(sort)
    filters = _b2b_filters(brand, _extract_attributes(request))

    # B2B publishes only in-stock products, so in_stock=false yields an empty
    # selection without asking B2B for something it cannot return.
    if in_stock_bool is False:
        return PaginatedCatalogProducts(items=[], total_count=0, limit=limit, offset=offset)

    page = await _b2b_call(
        b2b_client.list_public_products(
            category_id=category_id,
            search=safe_search,
            min_price=price_min,
            max_price=price_max,
            seller_id=seller_id,
            filters=filters,
            sort=b2b_sort,
            limit=limit,
            offset=offset,
        )
    )

    return PaginatedCatalogProducts(
        items=[_card_from_short(item) for item in page.items],
        total_count=page.total_count,
        limit=page.limit,
        offset=page.offset,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/products/{product_id}
# ---------------------------------------------------------------------------
@router.get("/products/{product_id}", response_model=CatalogProductDetail)
async def get_product(product_id: str):
    """Get full product details.

    B2B call: ``GET /public/products/{id}`` → ``ProductPublicResponse``.
    """
    product = await _b2b_call(b2b_client.get_public_product(product_id))

    if product is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )

    return _detail_from_product(product)


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/facets
# ---------------------------------------------------------------------------
@router.get("/facets", response_model=FacetsResponse)
async def get_facets(
    request: Request,
    category_id: Optional[str] = Query(None, alias="filter[category_id]", description="Category ID filter"),
    price_min: Optional[int] = Query(None, alias="filter[price_min]", ge=0, description="Minimum price, kopecks"),
    price_max: Optional[int] = Query(None, alias="filter[price_max]", ge=0, description="Maximum price, kopecks"),
    seller_id: Optional[str] = Query(None, alias="filter[seller_id]", description="Seller ID filter"),
    brand: Optional[str] = Query(None, alias="filter[brand]", description="Brand filter"),
    in_stock: Optional[str] = Query(None, alias="filter[in_stock]", description="in_stock=true/false"),
    q: Optional[str] = Query(None, max_length=SEARCH_MAX_LENGTH, description="Restrict counts to this query"),
    sort: Optional[str] = Query(None, description=f"Sort: {', '.join(ALLOWED_SORT_VALUES)}"),
    db: AsyncSession = Depends(get_db),
):
    """Get facet counts for the current filter context.

    The published B2B contract exposes no facets endpoint, so the counters are
    aggregated from the very selection B2B returns for these filters
    (``GET /public/products`` + ``POST /public/products/batch`` for brand
    characteristics) — see ``docs/adr-001-catalog-facets.md``.
    """
    in_stock_bool = _parse_in_stock(in_stock)
    safe_search = _validate_search(q)
    b2b_sort = _b2b_sort(sort)

    selection = FacetSelection(
        category_id=category_id,
        price_min=price_min,
        price_max=price_max,
        seller_id=seller_id,
        search=safe_search,
        in_stock=in_stock_bool,
        sort=b2b_sort,
        attributes=_sorted_attribute_items(_b2b_filters(brand, _extract_attributes(request))),
    )

    facets: List[FacetBucket] = await _b2b_call(compute_facets(selection))

    return FacetsResponse(category_id=category_id or "", facets=facets)


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/products/{product_id}/similar
# ---------------------------------------------------------------------------
SIMILAR_LIMIT: int = 8


@router.get("/products/{product_id}/similar", response_model=RecommendationList)
async def get_similar_products(
    product_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get similar products, excluding the current product.

    B2B call: ``GET /public/products/{id}/similar?limit=`` → an array of
    ``ProductPublicShortResponse``. That operation accepts only ``product_id``
    and ``limit`` — it has no ``category_id`` parameter, so the parent-category
    expansion from the canon flow cannot be requested from B2B today. If the
    category tree is reachable we still top the list up from the parent
    category via ``GET /public/products?category_id=...``; otherwise the buyer
    gets whatever the same-category selection returned.
    """
    current = await _b2b_call(b2b_client.get_public_product(product_id))

    if current is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
        )

    # Ask for one extra row: B2B may include the current product itself.
    shorts = await _b2b_call(
        b2b_client.get_public_similar_products(product_id, limit=SIMILAR_LIMIT + 1)
    )

    seen: set[str] = set()
    recommendations: List[ProductBasic] = []

    def _collect(items) -> None:
        for item in items:
            if item.id == product_id or item.id in seen:
                continue
            if len(recommendations) >= SIMILAR_LIMIT:
                return
            seen.add(item.id)
            recommendations.append(
                ProductBasic(
                    id=item.id,
                    name=item.title,
                    main_image_url=item.cover_image or "",
                    min_price=item.min_price,
                    has_stock=True,
                )
            )

    _collect(shorts)
    from_same_category = bool(recommendations)

    filled_from_parent = False
    if len(recommendations) < SIMILAR_LIMIT:
        parent_category_id = await _resolve_parent_category(current.category_id)
        if parent_category_id:
            try:
                page = await b2b_client.list_public_products(
                    category_id=parent_category_id,
                    sort="created_desc",
                    limit=(SIMILAR_LIMIT - len(recommendations)) * 2,
                    offset=0,
                )
            except B2BClientError:
                # Non-fatal — return what the same-category call produced.
                page = None
            if page is not None:
                before = len(recommendations)
                _collect(page.items)
                filled_from_parent = len(recommendations) > before

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


async def _resolve_parent_category(category_id: str) -> Optional[str]:
    """Look up the parent of a category in B2B's category list (None if unreachable)."""
    try:
        categories = await b2b_client.get_categories()
    except B2BClientError:
        return None
    for category in categories:
        if str(category.get("category_id", "")) == category_id:
            parent = category.get("parent_id")
            return str(parent) if parent else None
    return None


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
        cid = str(c.get("category_id", ""))
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
    """Build breadcrumbs path from root to target category.

    Raises ValueError on orphan node (parent_id points to non-existent category).
    """
    by_id: dict[str, dict] = {}
    for c in flat_categories:
        cid = str(c.get("category_id", ""))
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
            category_id=str(p.get("category_id", "")),
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
    """Get available filter values with counts for a category."""
    facets: List[FacetBucket] = await _b2b_call(
        compute_facets(FacetSelection(category_id=category_id))
    )
    filters_list = [
        FilterOption(name=fb.name, label=fb.label, values=fb.values) for fb in facets
    ]
    return ProductFilters(category_id=category_id, filters=filters_list)