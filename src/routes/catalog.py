"""Catalog routes — product listing with filters, sorting, facets, and pagination.

B2B integration rule: the storefront catalogue lives in B2B. Every request below
is built from the published B2B contract (``b2b/openapi.yaml``, tag
``Public Catalog``) and every response is read through
``src.services.b2b_public_catalog``. Visibility (MODERATED, not deleted,
active_quantity > 0) is applied by B2B — B2C never filters on those fields.
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.schemas import (
    CatalogProductCard,
    CatalogProductDetail,
    CatalogSku,
    CategoryRef,
    CategoryTreeNode,
    FacetBucket,
    FacetsResponse,
    FilterOption,
    FilterValue,
    ImageRef,
    PaginatedCatalogProducts,
    ProductFilters,
)
from src.services.b2b_client import b2b_client, B2BClientError
from src.services.b2b_public_catalog import B2BCategory, B2BProduct, B2BProductShort
from src.services.facet_service import FacetSelection, get_facets as compute_facets

router = APIRouter()

# Compatibility alias for the task's ``GET /api/v1/products?search=`` form. The
# published B2C contract exposes the same operation as
# ``GET /api/v1/catalog/products`` with the ``q`` parameter, so the alias stays
# out of the generated OpenAPI document and only mirrors the canonical handler.
alias_router = APIRouter()

# ---------------------------------------------------------------------------
# Search validation
# ---------------------------------------------------------------------------
SEARCH_MIN_LENGTH: int = 3
SEARCH_MAX_LENGTH: int = 200


def _validate_search(query: Optional[str]) -> Optional[str]:
    """Validate the search parameter and return it unchanged.

    Length rules mirror the B2B ``search`` param (minLength 3) and the B2C ``q``
    param (maxLength 200). The canon flow (``b2c-catalog-flows.md#b2c-2-search``)
    puts matching on B2B: B2C validates the length only and forwards the value
    exactly as received. No escaping, trimming or LIKE-pattern rewriting happens
    here — ``%``, ``_`` and quotes are B2B's business, and pre-escaping them
    corrupts the user's input.
    """
    if query is None or query.strip() == "":
        return None

    if len(query.strip()) < SEARCH_MIN_LENGTH:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SHORT_SEARCH_QUERY",
                "message": f"Search query must be at least {SEARCH_MIN_LENGTH} characters",
            },
        )
    if len(query) > SEARCH_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SEARCH_QUERY_TOO_LONG",
                "message": f"Search query must be at most {SEARCH_MAX_LENGTH} characters",
            },
        )
    return query


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
    and no image objects, while the B2C ``ImageRef`` contract requires ``id``,
    ``url`` and ``ordering`` and types ``id`` as a UUID. B2B supplies no id for
    the cover, so a stable UUIDv5 is derived from the product id and the cover
    URL: it keeps the response schema-valid and identical across requests
    without inventing random identifiers.
    """
    if not product.cover_image:
        return []
    return [
        ImageRef(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{product.id}:{product.cover_image}")),
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
            discount=sku.discount,
            available_quantity=sku.active_quantity,
            in_stock=sku.in_stock,
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


def _product_not_found() -> HTTPException:
    """404 for products the buyer must not see (missing, blocked, deleted)."""
    return HTTPException(
        status_code=404,
        detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
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
@router.get("/categories", response_model=List[CategoryRef])
async def get_categories():
    """Flat category list in the published B2C ``CategoryRef`` shape.

    B2B stores the tree as an adjacency list (``parent_id``); ``level`` and the
    breadcrumb ``path`` array are recomputed from those links. A broken
    hierarchy is a 422, not an empty or silently flattened list.
    """
    flat = await _b2b_call(b2b_client.get_categories())
    by_id = _category_index(flat)

    refs: List[CategoryRef] = []
    for category in flat:
        try:
            refs.append(_ref_from_chain(_category_chain(category.id, by_id)))
        except ValueError as exc:
            _raise_category_lookup_error(exc)
    return refs


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
    search: Optional[str] = Query(
        None,
        max_length=SEARCH_MAX_LENGTH,
        include_in_schema=False,
        description="Alias for q",
    ),
    sort: Optional[str] = Query(None, description=f"Sort: {', '.join(ALLOWED_SORT_VALUES)}"),
    limit: int = Query(20, ge=1, le=100, description="Items per page (1–100)"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    db: AsyncSession = Depends(get_db),
):
    """Get products with filtering, sorting, and pagination.

    Search follows the canon flow ``b2c-catalog-flows.md#b2c-2-search``: the raw
    ``q``/``search`` value is forwarded to B2B as ``search``, which matches
    ``title`` and ``description`` and applies the same visibility rules as the
    rest of the catalogue (MODERATED, not deleted, in stock). B2C validates only
    the length: shorter than 3 characters → 400, longer than 200 → 400.

    B2B call: ``GET /public/products`` with the spec's own parameter names —
    ``category_id``, ``search``, ``min_price``, ``max_price``, ``seller_id``,
    ``filters[...]``, ``sort``, ``limit``, ``offset``.
    """
    in_stock_bool = _parse_in_stock(in_stock)
    search_query = _validate_search(q if q is not None else search)
    b2b_sort = _b2b_sort(sort)
    filters = _b2b_filters(brand, _extract_attributes(request))

    # B2B publishes only in-stock products, so in_stock=false yields an empty
    # selection without asking B2B for something it cannot return.
    if in_stock_bool is False:
        return PaginatedCatalogProducts(items=[], total_count=0, limit=limit, offset=offset)

    page = await _b2b_call(
        b2b_client.list_public_products(
            category_id=category_id,
            search=search_query,
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
# Products B2B still returns but the storefront must not show: only MODERATED
# products are visible to buyers (the public B2B endpoint already hides them, so
# this is a defensive check that keeps blocked/deleted products at 404 even if
# the upstream filter regresses).
HIDDEN_PRODUCT_STATUSES: frozenset[str] = frozenset(
    {"CREATED", "ON_MODERATION", "BLOCKED", "HARD_BLOCKED"}
)


@router.get("/products/{product_id}", response_model=CatalogProductDetail)
async def get_product(product_id: str):
    """Get the storefront product card (US-CAT-03).

    B2B call: ``GET /public/products/{id}`` → ``ProductPublicResponse`` (fresh on
    every open, no local copy). A 404 from B2B, a missing product, or a product
    that is not MODERATED all surface as the same 404 ``PRODUCT_NOT_FOUND`` — the
    buyer must not be able to tell a blocked product from a deleted one.

    Response is mapped field-by-field through :func:`_detail_from_product`:
    ``cost_price``/``reserved_quantity`` and any other seller-internal field are
    never copied, because only an explicit allow-list of B2C fields is built.
    """
    product = await _b2b_call(b2b_client.get_public_product(product_id))

    if product is None or product.status in HIDDEN_PRODUCT_STATUSES:
        raise _product_not_found()

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
    search: Optional[str] = Query(
        None,
        max_length=SEARCH_MAX_LENGTH,
        include_in_schema=False,
        description="Alias for q",
    ),
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
    search_query = _validate_search(q if q is not None else search)
    b2b_sort = _b2b_sort(sort)

    selection = FacetSelection(
        category_id=category_id,
        price_min=price_min,
        price_max=price_max,
        seller_id=seller_id,
        search=search_query,
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


@router.get("/products/{product_id}/similar", response_model=List[CatalogProductCard])
async def get_similar_products(
    product_id: str,
    limit: int = Query(SIMILAR_LIMIT, ge=1, le=50),
):
    """Get similar products — ``GET /api/v1/catalog/products/{id}/similar``.

    Response is a plain array of ``CatalogProductCard`` (B2C ``openapi.yaml``,
    ``/catalog/products/{product_id}/similar``), not a wrapper object.

    Selection is proxied to B2B: ``GET /public/products/{id}/similar?limit=``
    returns ``[ProductPublicShortResponse]`` from the same category. We ask for
    one extra row because B2B may include the current product itself, then drop
    it and any duplicates.

    The canon flow requires the block to hold up to 8 items and to top the list
    up from the parent category when the same-category selection is shorter.
    ``limit`` defaults to 8 (the block size); the published contract allows the
    caller to widen the request up to 50. A missing/blocked current product is
    404. No similar products at all is a valid ``200 []`` — the front end hides
    the block.
    """
    current = await _b2b_call(b2b_client.get_public_product(product_id))

    if current is None or current.status in HIDDEN_PRODUCT_STATUSES:
        raise _product_not_found()

    # One extra row: B2B may include the current product. B2B caps `limit` at 50
    # (same maximum as the B2C contract), so the extra row is clamped.
    shorts = await _b2b_call(
        b2b_client.get_public_similar_products(product_id, limit=min(limit + 1, 50))
    )

    seen: set[str] = {product_id}
    cards: List[CatalogProductCard] = []

    def _collect(items) -> None:
        for item in items:
            if len(cards) >= limit:
                return
            if item.id in seen:
                continue
            seen.add(item.id)
            cards.append(_card_from_short(item))

    _collect(shorts)

    if len(cards) < limit:
        parent_category_id = await _resolve_parent_category(current.category_id)
        if parent_category_id:
            try:
                page = await b2b_client.list_public_products(
                    category_id=parent_category_id,
                    sort="created_desc",
                    limit=(limit - len(cards)) * 2,
                    offset=0,
                )
            except B2BClientError:
                # Non-fatal — return what the same-category call produced.
                page = None
            if page is not None:
                _collect(page.items)

    return cards


async def _resolve_parent_category(category_id: str) -> Optional[str]:
    """Look up the parent of a category in B2B's category list (None if unreachable)."""
    try:
        categories = await b2b_client.get_categories()
    except B2BClientError:
        return None
    for category in categories:
        if category.id == category_id:
            return category.parent_id
    return None


# ---------------------------------------------------------------------------
# Helpers for category navigation
# ---------------------------------------------------------------------------
class _UnknownCategory(ValueError):
    """The requested category is not present in the B2B category list."""


class _BrokenCategoryHierarchy(ValueError):
    """B2B returned an orphan node or a cycle."""


def _category_index(categories: List[B2BCategory]) -> dict[str, B2BCategory]:
    return {category.id: category for category in categories}


def _category_chain(category_id: str, by_id: dict[str, B2BCategory]) -> List[B2BCategory]:
    """Walk ``parent_id`` links from the category up to the root.

    Raises:
        _UnknownCategory — the id is absent from the B2B list (→ 404).
        _BrokenCategoryHierarchy — a parent is missing or the links form a
            cycle (→ 422): the tree cannot be represented truthfully.
    """
    if category_id not in by_id:
        raise _UnknownCategory(f"Category {category_id} not found")

    chain: List[B2BCategory] = []
    seen: set[str] = set()
    current = by_id[category_id]

    while True:
        if current.id in seen:
            raise _BrokenCategoryHierarchy(
                f"Cycle detected in category hierarchy at {current.id}"
            )
        seen.add(current.id)
        chain.append(current)

        if current.parent_id is None:
            break
        parent = by_id.get(current.parent_id)
        if parent is None:
            raise _BrokenCategoryHierarchy(
                f"Orphan node: {current.id} references missing parent {current.parent_id}"
            )
        current = parent

    chain.reverse()  # root → leaf
    return chain


def _ref_from_chain(chain: List[B2BCategory]) -> CategoryRef:
    """Build a B2C ``CategoryRef`` with the computed level and breadcrumb path."""
    leaf = chain[-1]
    return CategoryRef(
        id=leaf.id,
        name=leaf.name,
        parent_id=leaf.parent_id,
        level=len(chain) - 1,
        path=[category.name for category in chain],
    )


def _raise_category_lookup_error(exc: ValueError):
    """Map a broken/unknown category lookup to the B2C error contract."""
    if isinstance(exc, _UnknownCategory):
        raise HTTPException(
            status_code=404,
            detail={"code": "CATEGORY_NOT_FOUND", "message": str(exc)},
        ) from exc
    raise HTTPException(
        status_code=422,
        detail={"code": "ORPHAN_NODE", "message": str(exc)},
    ) from exc


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/categories/tree
# ---------------------------------------------------------------------------

@router.get("/categories/tree", response_model=List[CategoryTreeNode])
async def get_category_tree():
    """Get the full category tree as a nested structure.

    The tree is rebuilt from B2B's flat adjacency list. Every node carries the
    B2C ``CategoryRef`` fields (``id``/``name``/``parent_id``/``level``/
    ``path``) plus ``children``. A broken hierarchy (parent missing, cycle) is
    reported as 422 instead of silently re-parenting the node to the root.
    """
    flat = await _b2b_call(b2b_client.get_categories())
    by_id = _category_index(flat)

    nodes: dict[str, CategoryTreeNode] = {}
    for category in flat:
        try:
            ref = _ref_from_chain(_category_chain(category.id, by_id))
        except ValueError as exc:
            _raise_category_lookup_error(exc)
        nodes[category.id] = CategoryTreeNode(**ref.model_dump(), children=[])

    roots: List[CategoryTreeNode] = []
    for category in flat:
        node = nodes[category.id]
        if category.parent_id is None:
            roots.append(node)
        else:
            nodes[category.parent_id].children.append(node)

    return roots


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/categories/{category_id}
# ---------------------------------------------------------------------------

@router.get("/categories/{category_id}", response_model=CategoryRef)
async def get_category_detail(category_id: str):
    """Get details for a single category (``CategoryRef`` shape)."""
    flat = await _b2b_call(b2b_client.get_categories())
    by_id = _category_index(flat)

    try:
        chain = _category_chain(category_id, by_id)
    except ValueError as exc:
        _raise_category_lookup_error(exc)

    return _ref_from_chain(chain)


# ---------------------------------------------------------------------------
# GET /api/v1/catalog/breadcrumbs
# ---------------------------------------------------------------------------

@router.get("/breadcrumbs", response_model=List[CategoryRef])
async def get_breadcrumbs(
    category_id: Optional[str] = Query(None, description="Category ID for breadcrumbs"),
    product_id: Optional[str] = Query(None, description="Product ID — breadcrumbs built from its category"),
):
    """Build the breadcrumb chain from the root down to the target category.

    Accepts exactly one of ``category_id`` / ``product_id``:
    both → 400 ``AMBIGUOUS_PARAMS``; neither → 400 ``MISSING_PARAMS``.
    Unknown category → 404 ``CATEGORY_NOT_FOUND``; orphan node / cycle → 422
    ``ORPHAN_NODE``.

    The response reuses the published ``CategoryRef`` schema; each item carries
    its own ``level`` and ``path`` (breadcrumb names from the root).
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

    target_category_id = category_id

    if product_id:
        product = await _b2b_call(b2b_client.get_public_product(product_id))
        if product is None or product.status in HIDDEN_PRODUCT_STATUSES:
            raise HTTPException(
                status_code=404,
                detail={"code": "PRODUCT_NOT_FOUND", "message": "Product not found"},
            )
        target_category_id = product.category_id

    flat = await _b2b_call(b2b_client.get_categories())
    by_id = _category_index(flat)

    try:
        chain = _category_chain(target_category_id or "", by_id)
    except ValueError as exc:
        _raise_category_lookup_error(exc)

    return [_ref_from_chain(chain[: index + 1]) for index in range(len(chain))]


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


# ---------------------------------------------------------------------------
# Compatibility alias: GET /api/v1/products (same handler as
# /api/v1/catalog/products, hidden from the generated OpenAPI document)
# ---------------------------------------------------------------------------
alias_router.add_api_route(
    "/products",
    get_products,
    methods=["GET"],
    response_model=PaginatedCatalogProducts,
    include_in_schema=False,
)

alias_router.add_api_route(
    "/products/{product_id}",
    get_product,
    methods=["GET"],
    response_model=CatalogProductDetail,
    include_in_schema=False,
)

alias_router.add_api_route(
    "/products/{product_id}/similar",
    get_similar_products,
    methods=["GET"],
    response_model=List[CatalogProductCard],
    include_in_schema=False,
)
