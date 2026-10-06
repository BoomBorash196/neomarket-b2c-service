"""Facet counters for the storefront catalog (US-CAT-01).

The published B2B contract (``b2b/openapi.yaml``) has **no** facets endpoint,
and the public listing returns ``ProductPublicShortResponse`` — a card without
characteristics. So the counters are aggregated from exactly what B2B really
publishes for the buyer's current selection:

1. ``GET /public/products`` with the current filters — the same selection the
   buyer sees on ``GET /api/v1/catalog/products`` (bounded scan, pages of 100).
2. ``POST /public/products/batch`` for those product ids — the full
   ``ProductPublicResponse`` carries ``characteristics`` (that is where the
   brand lives) and per-SKU ``active_quantity``.

Both calls receive the current filter context, so counters always describe the
current selection instead of a stale/global set. Results are memoised for a few
seconds to keep the double fan-out off the hot path.

See ``docs/adr-001-catalog-facets.md`` for the alternatives that were weighed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from src.schemas import FacetBucket, FilterValue
from src.services.b2b_client import B2BClientError, b2b_client
from src.services.b2b_public_catalog import B2BProduct, B2BProductShort

logger = logging.getLogger(__name__)

# --- Bounds ---------------------------------------------------------------------
# How many products of the current selection are scanned for counters.
FACET_SCAN_LIMIT: int = 500
# B2B caps `limit` at 100 per request.
FACET_PAGE_SIZE: int = 100
# B2B caps `product_ids` at 100 per batch call.
FACET_BATCH_SIZE: int = 100
# Values returned per facet (the UI shows a "show all" link beyond that).
FACET_MAX_VALUES: int = 50
# Counters are memoised for this long — the scan is a fan-out, not a lookup.
FACET_CACHE_TTL_SECONDS: float = 30.0
FACET_CACHE_MAX_ENTRIES: int = 128

# --- Facet definitions ----------------------------------------------------------
# Price buckets in kopecks, matching the unit of `min_price` in the B2C/B2B specs.
PRICE_BUCKETS: tuple[tuple[int, Optional[int], str], ...] = (
    (0, 50_000, "0-50000"),
    (50_000, 100_000, "50000-100000"),
    (100_000, 500_000, "100000-500000"),
    (500_000, 1_000_000, "500000-1000000"),
    (1_000_000, None, "1000000+"),
)

PRICE_BUCKET_LABELS: dict[str, str] = {
    "0-50000": "до 500 ₽",
    "50000-100000": "500–1 000 ₽",
    "100000-500000": "1 000–5 000 ₽",
    "500000-1000000": "5 000–10 000 ₽",
    "1000000+": "от 10 000 ₽",
}

# Product characteristic names that carry the brand, in either locale.
BRAND_CHARACTERISTIC_NAMES: frozenset[str] = frozenset(
    {"brand", "бренд", "manufacturer", "производитель"}
)


@dataclass(frozen=True)
class FacetSelection:
    """Current filter context of the buyer (what the counters describe)."""

    category_id: Optional[str] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    seller_id: Optional[str] = None
    search: Optional[str] = None
    in_stock: Optional[bool] = None
    sort: str = "popular"
    attributes: tuple[tuple[str, str], ...] = ()

    def cache_key(self) -> tuple:
        return (
            self.category_id,
            self.price_min,
            self.price_max,
            self.seller_id,
            self.search,
            self.in_stock,
            self.sort,
            self.attributes,
        )


@dataclass
class _FacetCacheEntry:
    facets: list[FacetBucket]
    stored_at: float


_facet_cache: dict[tuple, _FacetCacheEntry] = {}


def clear_facet_cache() -> None:
    """Drop memoised counters (used by tests)."""
    _facet_cache.clear()


def _cache_get(key: tuple) -> Optional[list[FacetBucket]]:
    entry = _facet_cache.get(key)
    if entry is None:
        return None
    if time.monotonic() - entry.stored_at > FACET_CACHE_TTL_SECONDS:
        _facet_cache.pop(key, None)
        return None
    return [bucket.model_copy(deep=True) for bucket in entry.facets]


def _cache_put(key: tuple, facets: list[FacetBucket]) -> list[FacetBucket]:
    if len(_facet_cache) >= FACET_CACHE_MAX_ENTRIES:
        oldest = min(_facet_cache.items(), key=lambda kv: kv[1].stored_at)[0]
        _facet_cache.pop(oldest, None)
    _facet_cache[key] = _FacetCacheEntry(facets=[b.model_copy(deep=True) for b in facets], stored_at=time.monotonic())
    return facets


# ======================================================================
# Aggregation
# ======================================================================
def _bucket_for_price(min_price: int) -> str:
    for low, high, value in PRICE_BUCKETS:
        if min_price >= low and (high is None or min_price < high):
            return value
    return PRICE_BUCKETS[-1][2]


def _brand_of(product: B2BProduct) -> Optional[str]:
    for characteristic in product.characteristics:
        if characteristic.name.strip().lower() in BRAND_CHARACTERISTIC_NAMES:
            value = characteristic.value.strip()
            if value:
                return value
    return None


def _to_values(counter: Counter, labels: Optional[Mapping[str, str]] = None) -> list[FilterValue]:
    """Counters → FilterValue list, sorted by count desc then label asc."""
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], (labels or {}).get(kv[0], kv[0]).lower()))
    values: list[FilterValue] = []
    for value, count in ordered[:FACET_MAX_VALUES]:
        label = (labels or {}).get(value) or value
        values.append(FilterValue(value=value, label=label, count=count))
    return values


def build_facets(
    shorts: Iterable[B2BProductShort],
    cards: Mapping[str, B2BProduct],
) -> list[FacetBucket]:
    """Build facet buckets from the products B2B returned for this selection."""
    shorts = list(shorts)
    brands: Counter = Counter()
    prices: Counter = Counter()
    in_stock: Counter = Counter()

    for short in shorts:
        prices[_bucket_for_price(short.min_price)] += 1
        card = cards.get(short.id)
        if card is not None:
            in_stock["true" if card.in_stock else "false"] += 1
            brand = _brand_of(card)
            if brand:
                brands[brand.lower()] += 1
        else:
            # B2B public listing only contains products with active_quantity > 0,
            # so an item without a hydrated card is in stock by contract.
            in_stock["true"] += 1

    facets: list[FacetBucket] = []

    if brands:
        facets.append(
            FacetBucket(name="brand", label="Бренд", values=_to_values(brands))
        )

    facets.append(
        FacetBucket(
            name="price_range",
            label="Цена",
            values=_to_values(prices, PRICE_BUCKET_LABELS),
        )
    )

    facets.append(
        FacetBucket(
            name="in_stock",
            label="Наличие",
            values=_to_values(
                in_stock, {"true": "В наличии", "false": "Нет в наличии"}
            ),
        )
    )

    return facets


def empty_facets() -> list[FacetBucket]:
    """Counters for an empty selection (e.g. ``filter[in_stock]=false``)."""
    return [
        FacetBucket(name="price_range", label="Цена", values=[]),
        FacetBucket(name="in_stock", label="Наличие", values=[]),
    ]


# ======================================================================
# B2B calls
# ======================================================================
async def _scan_selection(selection: FacetSelection) -> list[B2BProductShort]:
    """Read the current selection from B2B (bounded number of pages)."""
    filters: dict[str, Any] = dict(selection.attributes)

    first = await b2b_client.list_public_products(
        category_id=selection.category_id,
        search=selection.search,
        min_price=selection.price_min,
        max_price=selection.price_max,
        seller_id=selection.seller_id,
        filters=filters or None,
        sort=selection.sort,
        limit=FACET_PAGE_SIZE,
        offset=0,
    )
    items: list[B2BProductShort] = list(first.items)
    target = min(FACET_SCAN_LIMIT, first.total_count)
    if len(items) >= target:
        return items

    offsets = list(range(FACET_PAGE_SIZE, target, FACET_PAGE_SIZE))
    pages = await asyncio.gather(
        *(
            b2b_client.list_public_products(
                category_id=selection.category_id,
                search=selection.search,
                min_price=selection.price_min,
                max_price=selection.price_max,
                seller_id=selection.seller_id,
                filters=filters or None,
                sort=selection.sort,
                limit=min(FACET_PAGE_SIZE, target - offset),
                offset=offset,
            )
            for offset in offsets
        )
    )
    for page in pages:
        items.extend(page.items)
    return items[:target]


async def _hydrate_cards(product_ids: list[str]) -> dict[str, B2BProduct]:
    """Load full cards (characteristics + per-SKU stock) in batches of 100."""
    chunks = [
        product_ids[i : i + FACET_BATCH_SIZE]
        for i in range(0, len(product_ids), FACET_BATCH_SIZE)
    ]
    if not chunks:
        return {}
    batches = await asyncio.gather(
        *(b2b_client.batch_public_products(chunk) for chunk in chunks)
    )
    cards: dict[str, B2BProduct] = {}
    for batch in batches:
        for card in batch:
            cards[card.id] = card
    return cards


async def get_facets(selection: FacetSelection) -> list[FacetBucket]:
    """Facet counters for the current selection.

    The B2B public contract exposes no way to ask for "out of stock" products
    (``/public/products`` returns ``active_quantity > 0`` only), so
    ``in_stock=False`` is materialised locally as an empty selection instead of
    being silently dropped.
    """
    if selection.in_stock is False:
        return empty_facets()

    key = selection.cache_key()
    cached = _cache_get(key)
    if cached is not None:
        logger.debug("facets: cache hit for %s", key)
        return cached

    try:
        shorts = await _scan_selection(selection)
        cards = await _hydrate_cards([short.id for short in shorts])
    except B2BClientError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise B2BClientError(
            status_code=502, message=f"facet aggregation failed: {exc}", kind="protocol"
        ) from exc

    facets = build_facets(shorts, cards)
    return _cache_put(key, facets)