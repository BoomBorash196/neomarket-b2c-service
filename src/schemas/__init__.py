"""Pydantic schemas for request/response validation."""

from pydantic import BaseModel, Field
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
    images: List[str]
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
    """Cart item with product details."""
    cart_item_id: int
    sku_id: str
    product_id: str
    product_title: str
    sku_info: SKUInfo
    quantity: int
    subtotal: float


class Cart(BaseModel):
    """User cart with totals."""
    user_id: str
    items: List[CartItem]
    total_items: int
    total_amount: float


class UnavailableCartItem(BaseModel):
    """Cart item that is unavailable (out of stock or inactive).

    Shown in the response so the buyer sees what they added,
    but excluded from total_amount.  unavailable_reason is computed
    live from B2B — never stored in DB.
    """
    cart_item_id: int
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


# --- Order schemas ---
class OrderCreate(BaseModel):
    """Create order from cart — no items/total needed; server computes from B2B."""
    user_id: str


class OrderItem(BaseModel):
    """Order item details — historical snapshot at purchase time."""
    order_item_id: int
    order_id: int
    sku_id: str
    sku_name: str
    product_id: str
    product_title: str
    quantity: int
    unit_price: float


class Order(BaseModel):
    """Order with status and items."""
    order_id: int
    user_id: str
    status: OrderStatus
    total_amount: float
    created_at: datetime
    items: List[OrderItem]


class OrderList(BaseModel):
    """Paginated order list."""
    orders: List[Order]
    total: int
    page: int
    page_size: int


class OrderStatusUpdate(BaseModel):
    """Internal status update (logistics / admin)."""
    status: OrderStatus


# --- Wishlist schemas ---
class WishlistItemCreate(BaseModel):
    """Add to wishlist request."""
    product_id: str


class WishlistItem(BaseModel):
    """Wishlist item with product info."""
    wishlist_item_id: int
    user_id: str
    product_id: str
    product_title: str
    main_image_url: str
    min_price: float
    is_available: bool
    added_at: datetime


class Wishlist(BaseModel):
    """User wishlist."""
    user_id: str
    items: List[WishlistItem]


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
    subscription_id: int
    user_id: str
    sku_id: str
    notify_on: str
    created_at: datetime


# --- Category schemas ---
class CategoryNode(BaseModel):
    """Category tree node."""
    category_id: str
    name: str
    parent_id: Optional[str] = None
    children: List["CategoryNode"] = []


class CategoryDetail(BaseModel):
    """Detailed info about a single category."""
    category_id: str
    name: str
    parent_id: Optional[str] = None


class BreadcrumbItem(BaseModel):
    """A single item in the breadcrumbs chain."""
    category_id: str
    name: str
    parent_id: Optional[str] = None


class BreadcrumbsResponse(BaseModel):
    """Breadcrumbs path from root to target category."""
    items: List[BreadcrumbItem]


# --- Collection/Banner schemas ---
class Banner(BaseModel):
    """Promotional banner."""
    banner_id: int
    title: str
    image_url: str
    link_url: str
    priority: int
    is_active: bool
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None


class BannerClickCreate(BaseModel):
    """Record a banner click for CTR analytics."""
    banner_id: int
    user_id: Optional[str] = None


class CollectionProduct(BaseModel):
    """Product in a collection."""
    product_id: str
    title: str
    main_image_url: str
    min_price: float


class Collection(BaseModel):
    """Product collection (e.g., "Hit of the season")."""
    collection_id: int
    title: str
    description: Optional[str] = None
    products: List[CollectionProduct]
    max_display: int = 8
    unavailable_ids: List[str] = []  # product_ids unavailable in B2B


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
class ProductDetailSchema(BaseModel):
    """B2C-safe product detail returned in catalog listings."""
    id: str
    name: str
    main_image_url: str
    min_price: float
    has_stock: bool
    description: str
    images: List[str]
    characteristics: dict[str, str]
    skus: List["SKUInfo"]


class PaginatedCatalogProducts(BaseModel):
    """Paginated catalog product list per OpenAPI spec."""
    items: List[ProductDetailSchema]
    total_count: int
    limit: int
    offset: int


# --- Recommendation schemas ---
class RecommendationList(BaseModel):
    """Recommended products."""
    current_product_id: str
    recommendations: List[ProductBasic]
    reason: str  # e.g., "same_category", "parent_category"