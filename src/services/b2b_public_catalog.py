"""Strict readers for the B2B *public catalog* payloads.

Every field name and shape below is taken from the published B2B contract
(``neomarket-protocols → b2b/openapi.yaml``, tag ``Public Catalog``):

===================================  ==================================
B2B operation                        Payload schema
===================================  ==================================
``GET  /public/products``            ``ProductPublicPaginatedResponse``
``GET  /public/products/{id}``       ``ProductPublicResponse``
``GET  /public/products/{id}/similar``  ``[ProductPublicShortResponse]``
``POST /public/products/batch``      ``[ProductPublicResponse]``
===================================  ==================================

The readers are intentionally strict. A payload that does not match the
published schema is a protocol violation on the B2B side; it is reported as
such (and surfaces to the buyer as 502) instead of being silently coerced into
an empty catalog — silently returning "no products" is what made the previous
B2B-contract mismatch invisible.

There is no facets endpoint in the published B2B contract, so facet counters are
aggregated from what B2B really returns (see ``src/services/facet_service.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "B2BPayloadError",
    "B2BImage",
    "B2BCharacteristic",
    "B2BSku",
    "B2BProductShort",
    "B2BProductPage",
    "B2BProduct",
    "B2B_SORT_VALUES",
    "parse_product_short",
    "parse_product_shorts",
    "parse_product_page",
    "parse_product",
    "parse_products",
]


class B2BPayloadError(ValueError):
    """Raised when a B2B payload does not match the published B2B schema."""


# --- B2B /public/products → sort ------------------------------------------------
# enum from b2b/openapi.yaml (B2B side has no "popularity"/"new" aliases).
B2B_SORT_VALUES: tuple[str, ...] = ("price_asc", "price_desc", "created_desc", "popular")

# --- ProductStatus enum ----------------------------------------------------------
B2B_PRODUCT_STATUSES: frozenset[str] = frozenset(
    {"CREATED", "ON_MODERATION", "MODERATED", "BLOCKED", "HARD_BLOCKED"}
)


# ======================================================================
# Primitive readers
# ======================================================================
def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise B2BPayloadError(f"{path}: expected object, got {type(value).__name__}")
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)):
        raise B2BPayloadError(f"{path}: expected array, got {type(value).__name__}")
    return value


def _req_str(obj: Mapping[str, Any], key: str, path: str) -> str:
    if key not in obj:
        raise B2BPayloadError(f"{path}.{key}: field is required by the B2B schema")
    value = obj[key]
    if not isinstance(value, str):
        raise B2BPayloadError(f"{path}.{key}: expected string, got {type(value).__name__}")
    return value


def _opt_str(obj: Mapping[str, Any], key: str, path: str) -> str | None:
    """Nullable string field (e.g. ``cover_image``, ``article``, ``parent_id``)."""
    if key not in obj or obj[key] is None:
        return None
    value = obj[key]
    if not isinstance(value, str):
        raise B2BPayloadError(f"{path}.{key}: expected string or null, got {type(value).__name__}")
    return value


def _req_int(obj: Mapping[str, Any], key: str, path: str) -> int:
    if key not in obj:
        raise B2BPayloadError(f"{path}.{key}: field is required by the B2B schema")
    value = obj[key]
    if isinstance(value, bool):
        raise B2BPayloadError(f"{path}.{key}: expected integer, got bool")
    if isinstance(value, int):
        return value
    # Tolerate JSON numbers that arrive as float (100 vs 100.0).
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise B2BPayloadError(f"{path}.{key}: expected integer, got {type(value).__name__}")


def _req_status(obj: Mapping[str, Any], key: str, path: str) -> str:
    status = _req_str(obj, key, path)
    if status not in B2B_PRODUCT_STATUSES:
        raise B2BPayloadError(
            f"{path}.{key}: '{status}' is not in the B2B ProductStatus enum"
        )
    return status


# ======================================================================
# Value objects
# ======================================================================
@dataclass(frozen=True)
class B2BImage:
    """``ProductImageResponse`` / ``SKUImageResponse``: {id, url, ordering}."""

    id: str
    url: str
    ordering: int


@dataclass(frozen=True)
class B2BCharacteristic:
    """``CharacteristicResponse``: {id, name, value}."""

    id: str
    name: str
    value: str


@dataclass(frozen=True)
class B2BSku:
    """``SKUPublicResponse`` — seller-internal fields are not part of it."""

    id: str
    product_id: str
    name: str
    price: int
    discount: int
    stock_quantity: int
    active_quantity: int
    article: str | None
    images: list[B2BImage] = field(default_factory=list)
    characteristics: list[B2BCharacteristic] = field(default_factory=list)

    @property
    def in_stock(self) -> bool:
        return self.active_quantity > 0

    @property
    def final_price(self) -> int:
        """Buyer-facing price: ``price`` minus the absolute ``discount``."""
        return self.price - self.discount


@dataclass(frozen=True)
class B2BProductShort:
    """``ProductPublicShortResponse`` — what the public listing returns."""

    id: str
    title: str
    slug: str
    status: str
    category_id: str
    min_price: int
    cover_image: str | None
    created_at: str


@dataclass(frozen=True)
class B2BProductPage:
    """``ProductPublicPaginatedResponse``: {items, total_count, limit, offset}."""

    items: list[B2BProductShort]
    total_count: int
    limit: int
    offset: int


@dataclass(frozen=True)
class B2BProduct:
    """``ProductPublicResponse`` — full storefront card for one product."""

    id: str
    seller_id: str
    category_id: str
    title: str
    slug: str
    description: str
    status: str
    images: list[B2BImage]
    characteristics: list[B2BCharacteristic]
    skus: list[B2BSku]
    created_at: str
    updated_at: str

    @property
    def in_stock(self) -> bool:
        return any(sku.in_stock for sku in self.skus)

    @property
    def min_price(self) -> int:
        """Lowest buyer-facing price among in-stock SKUs (kopecks)."""
        active = [sku.final_price for sku in self.skus if sku.in_stock]
        if active:
            return min(active)
        return min((sku.final_price for sku in self.skus), default=0)


# ======================================================================
# Composite readers
# ======================================================================
def _parse_image(raw: Any, path: str) -> B2BImage:
    obj = _mapping(raw, path)
    return B2BImage(
        id=_req_str(obj, "id", path),
        url=_req_str(obj, "url", path),
        ordering=_req_int(obj, "ordering", path),
    )


def _parse_characteristic(raw: Any, path: str) -> B2BCharacteristic:
    obj = _mapping(raw, path)
    return B2BCharacteristic(
        id=_req_str(obj, "id", path),
        name=_req_str(obj, "name", path),
        value=_req_str(obj, "value", path),
    )


def _parse_sku(raw: Any, path: str) -> B2BSku:
    obj = _mapping(raw, path)
    return B2BSku(
        id=_req_str(obj, "id", path),
        product_id=_req_str(obj, "product_id", path),
        name=_req_str(obj, "name", path),
        price=_req_int(obj, "price", path),
        discount=_req_int(obj, "discount", path),
        stock_quantity=_req_int(obj, "stock_quantity", path),
        active_quantity=_req_int(obj, "active_quantity", path),
        article=_opt_str(obj, "article", path),
        images=[_parse_image(i, f"{path}.images[{i}]") for i in _sequence(obj.get("images", []), f"{path}.images")],
        characteristics=[
            _parse_characteristic(c, f"{path}.characteristics[{i}]")
            for i, c in enumerate(_sequence(obj.get("characteristics", []), f"{path}.characteristics"))
        ],
    )


def parse_product_short(raw: Any, path: str = "product") -> B2BProductShort:
    """Read ``ProductPublicShortResponse``."""
    obj = _mapping(raw, path)
    return B2BProductShort(
        id=_req_str(obj, "id", path),
        title=_req_str(obj, "title", path),
        slug=_req_str(obj, "slug", path),
        status=_req_status(obj, "status", path),
        category_id=_req_str(obj, "category_id", path),
        min_price=_req_int(obj, "min_price", path),
        cover_image=_opt_str(obj, "cover_image", path),
        created_at=_req_str(obj, "created_at", path),
    )


def parse_product_shorts(raw: Any, path: str = "products") -> list[B2BProductShort]:
    """Read an array of ``ProductPublicShortResponse``."""
    return [parse_product_short(item, f"{path}[{i}]") for i, item in enumerate(_sequence(raw, path))]


def parse_product_page(raw: Any, path: str = "page") -> B2BProductPage:
    """Read ``ProductPublicPaginatedResponse``."""
    obj = _mapping(raw, path)
    return B2BProductPage(
        items=parse_product_shorts(obj.get("items", []), f"{path}.items"),
        total_count=_req_int(obj, "total_count", path),
        limit=_req_int(obj, "limit", path),
        offset=_req_int(obj, "offset", path),
    )


def parse_product(raw: Any, path: str = "product") -> B2BProduct:
    """Read ``ProductPublicResponse``."""
    obj = _mapping(raw, path)
    images_raw = _sequence(obj.get("images", []), f"{path}.images")
    chars_raw = _sequence(obj.get("characteristics", []), f"{path}.characteristics")
    skus_raw = _sequence(obj.get("skus", []), f"{path}.skus")
    return B2BProduct(
        id=_req_str(obj, "id", path),
        seller_id=_req_str(obj, "seller_id", path),
        category_id=_req_str(obj, "category_id", path),
        title=_req_str(obj, "title", path),
        slug=_req_str(obj, "slug", path),
        description=_req_str(obj, "description", path),
        status=_req_status(obj, "status", path),
        images=[_parse_image(i, f"{path}.images[{i}]") for i in images_raw],
        characteristics=[
            _parse_characteristic(c, f"{path}.characteristics[{i}]") for i, c in enumerate(chars_raw)
        ],
        skus=[_parse_sku(s, f"{path}.skus[{i}]") for i, s in enumerate(skus_raw)],
        created_at=_req_str(obj, "created_at", path),
        updated_at=_req_str(obj, "updated_at", path),
    )


def parse_products(raw: Any, path: str = "products") -> list[B2BProduct]:
    """Read an array of ``ProductPublicResponse``."""
    return [parse_product(item, f"{path}[{i}]") for i, item in enumerate(_sequence(raw, path))]