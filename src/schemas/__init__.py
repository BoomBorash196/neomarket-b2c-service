"""Pydantic schemas for request/response validation."""

from pydantic import BaseModel, Field, model_validator
from typing import Optional, List
from datetime import datetime
from enum import Enum


class OrderStatus(str, Enum):
    """Order lifecycle statuses."""
    CREATED = "CREATED"
    PAID = "PAID"
    ASSEMBLING = "ASSEMBLING"
    DELIVERING = "DELIVERING"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    CANCEL_PENDING = "CANCEL_PENDING"  # unreserve failed, retry scheduled


# --- Product schemas (from B2B) ---
class ImageRef(BaseModel):
    """Изображение товара по контракту ImageRef из B2C openapi.yaml.

    Обязательные поля: id, url, ordering. Необязательные: alt, is_main.
    Структурированный вид должен сохраняться при маппинге в B2C —
    изображения никогда не сплющиваются в простые строки URL
    ни в списке каталога, ни в карточке товара.
    """
    id: str
    url: str
    ordering: int = 0
    alt: Optional[str] = None
    is_main: Optional[bool] = None


class ProductBasic(BaseModel):
    """Basic product info from B2B."""
    id: str
    name: str
    main_image_url: str
    min_price: float
    has_stock: bool


class ProductDetail(ProductBasic):
    """Full product details."""
    description: str
    images: List[ImageRef]
    characteristics: dict[str, str]
    skus: List["SKUInfo"]


class SKUInfo(BaseModel):
    """SKU (variant) information — B2C-safe view.

    Only fields that buyers are allowed to see.
    Sensitive seller data (cost_price, reserved_quantity, etc.) is explicitly excluded.
    """
    id: str
    color: Optional[str] = None
    size: Optional[str] = None
    other_specs: Optional[dict[str, str]] = None
    price: float
    available_quantity: int
    is_active: bool
    in_stock: bool = True
    discount: float = 0.0


# --- Cart schemas ---
class CartItemCreate(BaseModel):
    """Add item to cart request."""
    sku_id: str
    quantity: int = Field(ge=1, le=999)


class CartItem(BaseModel):
    """Cart item with product details.

    OpenAPI ``CartItem`` requires ``sku_id``, ``product_id``, ``name``,
    ``quantity``, ``unit_price``, ``line_total``, ``available_quantity``,
    ``is_available``. Legacy fields (``cart_item_id``, ``product_title``,
    ``sku_info``, ``subtotal``) are kept for older clients.
    """
    cart_item_id: str
    sku_id: str
    product_id: str
    product_title: str
    sku_info: SKUInfo
    quantity: int
    subtotal: float
    name: str = ""
    unit_price: int = 0
    line_total: int = 0
    available_quantity: int = 0
    is_available: bool = True
    sku_code: Optional[str] = None
    unit_price_at_add: Optional[int] = None
    image: Optional[ImageRef] = None


class Cart(BaseModel):
    """User cart with totals."""
    user_id: str
    items: List[CartItem]
    total_items: int
    total_amount: float
    id: Optional[str] = None
    items_count: int = 0
    subtotal: float = 0.0
    is_valid: bool = True
    updated_at: Optional[datetime] = None


class UnavailableCartItem(BaseModel):
    """Cart item that is unavailable (out of stock or inactive).

    Shown in the response so the buyer sees what they added,
    but excluded from total_amount.  unavailable_reason is computed
    live from B2B — never stored in DB.
    """
    cart_item_id: str
    sku_id: str
    quantity: int
    product_id: str
    product_title: str
    price: float
    unavailable_reason: str  # "out_of_stock" | "not_active" | "sku_not_found"


class CartWithUnavailable(BaseModel):
    """Cart response with both available and unavailable items."""
    user_id: str
    items: List[CartItem]
    total_items: int
    total_amount: float
    unavailable: List[UnavailableCartItem] = []
    id: Optional[str] = None
    items_count: int = 0
    subtotal: float = 0.0
    is_valid: bool = True
    updated_at: Optional[datetime] = None


# --- Order schemas ---
class AddressResponse(BaseModel):
    """Shipping address snapshot on an order (OpenAPI ``AddressResponse``).

    ``country/city/street/building`` are required by the contract; the rest
    are optional. Checkout stores only ``address_id``, so absent fields are
    synthesised from it.
    """
    id: str
    country: str
    city: str
    street: str
    building: str
    created_at: datetime
    region: Optional[str] = None
    apartment: Optional[str] = None
    postal_code: Optional[str] = None
    recipient_name: Optional[str] = None
    recipient_phone: Optional[str] = None
    is_default: bool = False
    comment: Optional[str] = None


class PaymentMethodResponse(BaseModel):
    """Mock payment method snapshot (OpenAPI ``PaymentMethodResponse``)."""
    id: str
    type: str
    created_at: datetime
    card_last4: Optional[str] = None
    card_brand: Optional[str] = None
    is_default: bool = False


class OrderStatusEntry(BaseModel):
    """One entry of an order status history."""
    status: OrderStatus
    changed_at: datetime
    reason: Optional[str] = None


class OrderSnapshotItem(BaseModel):
    """Optional client-side cart snapshot used to detect races."""
    sku_id: str
    quantity: int = Field(ge=1)
    unit_price: int = Field(ge=0)


class OrderCreate(BaseModel):
    """Checkout request (OpenAPI ``OrderCreateRequest``).

    ``address_id`` / ``payment_method_id`` are the contract fields.
    ``user_id`` is accepted for backwards compatibility with callers that do
    not send the trusted ``X-User-Id`` header.
    """
    address_id: Optional[str] = None
    payment_method_id: Optional[str] = None
    comment: Optional[str] = None
    items_snapshot: Optional[List[OrderSnapshotItem]] = None
    user_id: Optional[str] = None


class OrderItem(BaseModel):
    """Order item details — historical snapshot at purchase time.

    Carries both the OpenAPI-required fields (``name``, ``line_total``) and
    the legacy aliases (``sku_name``, ``product_title``) used by older clients.
    """
    order_item_id: str
    order_id: str
    sku_id: str
    product_id: str
    name: str
    sku_name: str
    product_title: str
    quantity: int
    unit_price: int
    line_total: int


class Order(BaseModel):
    """Order with status and items.

    All OpenAPI ``OrderResponse`` required fields are present: ``id``,
    ``buyer_id``, ``status``, ``items``, ``subtotal``, ``total``, ``address``,
    ``created_at``. Legacy fields (``order_id``, ``user_id``, ``total_amount``)
    stay for compatibility.
    """
    id: str
    order_id: str
    number: Optional[str] = None
    buyer_id: str
    user_id: str
    status: OrderStatus
    items: List[OrderItem]
    subtotal: int
    delivery_cost: int = 0
    total: int
    total_amount: float
    address: AddressResponse
    payment_method: Optional[PaymentMethodResponse] = None
    comment: Optional[str] = None
    cancel_reason: Optional[str] = None
    status_history: List[OrderStatusEntry] = []
    created_at: datetime
    paid_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None


class OrderList(BaseModel):
    """Paginated order list (OpenAPI ``PaginatedOrders``)."""
    items: List[Order]
    total_count: int
    limit: int
    offset: int


# --- Wishlist schemas ---
class WishlistItemCreate(BaseModel):
    """Add to wishlist request."""
    product_id: str


class WishlistItem(BaseModel):
    """Wishlist item with product info.

    OpenAPI ``PaginatedCatalogProducts`` items are ``CatalogProductCard``:
    ``id``, ``name``, ``min_price``, ``has_stock``, ``images`` are required.
    Legacy wishlist fields are kept and mirrored into the contract fields.
    """
    wishlist_item_id: str
    user_id: str
    product_id: str
    product_title: str
    main_image_url: str
    min_price: float
    is_available: bool
    added_at: datetime
    id: str = ""
    name: str = ""
    has_stock: bool = False
    images: List[ImageRef] = []

    @model_validator(mode="after")
    def _fill_contract_fields(self):
        if not self.id:
            self.id = self.product_id
        if not self.name:
            self.name = self.product_title
        self.has_stock = self.is_available
        if not self.images and self.main_image_url:
            self.images = [
                ImageRef(id=self.product_id, url=self.main_image_url, ordering=0)
            ]
        return self


class Wishlist(BaseModel):
    """User wishlist (OpenAPI ``PaginatedCatalogProducts`` envelope)."""
    user_id: str
    items: List[WishlistItem]
    total_count: int = 0
    limit: int = 20
    offset: int = 0


# --- Subscription schemas ---
class NotifyOn(str, Enum):
    """Types of product events to subscribe to."""
    IN_STOCK = "in_stock"  # product is back in stock
    LOW_STOCK = "low_stock"  # product stock is running low


class SubscriptionCreate(BaseModel):
    """Create a product availability subscription."""
    sku_id: str
    notify_on: NotifyOn


class Subscription(BaseModel):
    """Subscription response."""
    subscription_id: str
    user_id: str
    sku_id: str
    notify_on: str
    created_at: datetime


# --- Category schemas ---
class CategoryRef(BaseModel):
    """B2C ``CategoryRef`` (openapi.yaml:995).

    ``path`` is the breadcrumb chain from the root to this category (names),
    computed from the B2B adjacency list; ``level`` is the depth (root = 0).
    The published B2C schema types ``path`` as an array of strings — B2B's own
    ``path`` is a materialized string (``electronics/phones``), so the array is
    derived on the B2C side.
    """
    id: str
    name: str
    parent_id: Optional[str] = None
    level: int = 0
    path: List[str] = []


class CategoryTreeNode(CategoryRef):
    """B2C ``CategoryTreeNode`` (openapi.yaml:1008): CategoryRef + children."""
    children: List["CategoryTreeNode"] = []


# --- Collection/Banner schemas ---
class Banner(BaseModel):
    """Promotional banner.

    OpenAPI ``Banner`` requires ``id``, ``image_url`` and ``link``; the
    legacy names (``banner_id``, ``link_url``, ``priority``, ``is_active``)
    are kept for the existing home-page clients.
    """
    banner_id: str
    title: str
    image_url: str
    link_url: str = ""
    priority: int
    is_active: bool
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    id: str = ""
    link: str = ""
    ordering: int = 0
    active_from: Optional[datetime] = None
    active_to: Optional[datetime] = None

    @model_validator(mode="after")
    def _fill_contract_fields(self):
        if not self.id:
            self.id = self.banner_id
        self.link_url = self.link_url or ""
        if not self.link:
            self.link = self.link_url
        self.ordering = self.priority
        self.active_from = self.starts_at
        self.active_to = self.ends_at
        return self


class BannerClickCreate(BaseModel):
    """Record a banner click for CTR analytics."""
    banner_id: str
    user_id: Optional[str] = None


class CollectionProduct(BaseModel):
    """Product in a collection (OpenAPI ``CatalogProductCard`` required fields)."""
    product_id: str
    title: str
    main_image_url: str
    min_price: float
    id: str = ""
    name: str = ""
    has_stock: bool = True
    images: List[ImageRef] = []

    @model_validator(mode="after")
    def _fill_contract_fields(self):
        if not self.id:
            self.id = self.product_id
        if not self.name:
            self.name = self.title
        if not self.images and self.main_image_url:
            self.images = [
                ImageRef(id=self.product_id, url=self.main_image_url, ordering=0)
            ]
        return self


class Collection(BaseModel):
    """Product collection (e.g., "Hit of the season").

    OpenAPI ``Collection`` requires ``id``, ``name`` and ``products``; legacy
    ``collection_id`` / ``title`` are kept.
    """
    collection_id: str
    title: str
    description: Optional[str] = None
    products: List[CollectionProduct]
    max_display: int = 8
    unavailable_ids: List[str] = []
    id: str = ""
    name: str = ""

    @model_validator(mode="after")
    def _fill_contract_fields(self):
        if not self.id:
            self.id = self.collection_id
        if not self.name:
            self.name = self.title
        return self


# --- Filter / Facet schemas ---
class FilterValue(BaseModel):
    """A single filter value with count."""
    value: str
    label: str
    count: int


class FilterOption(BaseModel):
    """Available filter option."""
    name: str
    label: str
    values: List[FilterValue]


class ProductFilters(BaseModel):
    """Available filters for a category."""
    category_id: str
    filters: List[FilterOption]


class FacetBucket(BaseModel):
    """A single facet field with its value buckets."""
    name: str
    label: str
    values: List[FilterValue]


class FacetsResponse(BaseModel):
    """Response with facet counts for the current filter context."""
    category_id: str
    facets: List[FacetBucket]


# --- Catalog list / pagination schemas ---
class CatalogSku(BaseModel):
    """Storefront SKU — B2C ``CatalogSku`` (openapi.yaml:1084).

    Note: the B2B public SKU schema has no ``color``/``size`` columns; variant
    attributes live in ``characteristics`` and are exposed as ``attributes``.

    ``in_stock`` and ``discount`` are additive fields required by the canon flow
    ``b2c-catalog-flows.md#b2c-3-product-card``: a SKU with no active stock is
    still listed, but with ``in_stock=false``; ``discount > 0`` (absolute amount
    in kopecks, exactly as B2B reports it) tells the front end to show the
    crossed-out price. ``old_price`` carries the same fact for readers that only
    follow the published ``CatalogSku`` schema.
    """
    id: str
    name: Optional[str] = None
    sku_code: Optional[str] = None
    price: int
    old_price: Optional[int] = None
    discount: int = 0
    available_quantity: int
    in_stock: bool = True
    attributes: dict[str, str] = {}
    images: List[ImageRef] = []


class CatalogProductCard(BaseModel):
    """Storefront product card — B2C ``CatalogProductCard`` (openapi.yaml:1038).

    Required by spec: ``id``, ``name``, ``min_price``, ``has_stock``, ``images``.
    """
    id: str
    name: str
    slug: Optional[str] = None
    min_price: int
    has_stock: bool
    images: List[ImageRef] = []


class CatalogProductDetail(CatalogProductCard):
    """Full storefront card — B2C ``CatalogProductDetail`` (card + description, skus)."""
    description: str
    attributes: dict[str, str] = {}
    skus: List[CatalogSku] = []


class PaginatedCatalogProducts(BaseModel):
    """Paginated catalog product list per OpenAPI spec."""
    items: List[CatalogProductCard]
    total_count: int
    limit: int
    offset: int


# --- Recommendation schemas ---
class RecommendationList(BaseModel):
    """Recommended products."""
    current_product_id: str
    recommendations: List[ProductBasic]
    reason: str  # e.g., "same_category", "parent_category"