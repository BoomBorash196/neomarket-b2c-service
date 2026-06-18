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


# --- Product schemas (from B2B) ---
class ProductBasic(BaseModel):
    """Basic product info from B2B."""
    product_id: str
    title: str
    main_image_url: str
    min_price: float
    is_available: bool


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
    sku_id: str
    color: Optional[str] = None
    size: Optional[str] = None
    other_specs: Optional[dict[str, str]] = None
    price: float
    quantity_available: int
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
class OrderItemCreate(BaseModel):
    """Order item from cart."""
    sku_id: str
    quantity: int
    price_at_order: float


class OrderCreate(BaseModel):
    """Create order from cart."""
    user_id: str
    items: List[OrderItemCreate]
    total_amount: float


class OrderItem(BaseModel):
    """Order item details."""
    order_item_id: int
    order_id: int
    sku_id: str
    product_title: str
    quantity: int
    price: float


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


# --- Category schemas ---
class CategoryNode(BaseModel):
    """Category tree node."""
    category_id: str
    name: str
    parent_id: Optional[str] = None
    children: List["CategoryNode"] = []


# --- Collection/Banner schemas ---
class Banner(BaseModel):
    """Promotional banner."""
    banner_id: int
    title: str
    image_url: str
    link_url: str
    priority: int
    is_active: bool


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
class ProductListResponse(BaseModel):
    """Paginated product list with optional applied filters."""
    products: List[ProductDetail]
    total: int
    page: int
    page_size: int
    filters_applied: Optional[dict] = None


# --- Recommendation schemas ---
class RecommendationList(BaseModel):
    """Recommended products."""
    current_product_id: str
    recommendations: List[ProductBasic]
    reason: str  # e.g., "same_category", "parent_category"
