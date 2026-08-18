"""SQLAlchemy ORM models."""

import uuid

from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text, Enum, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from src.database import Base
from src.schemas import OrderStatus


def _uuid_str():
    """Generate a UUID string for default primary keys."""
    return str(uuid.uuid4())


class CartItemModel(Base):
    """Shopping cart items."""
    __tablename__ = "cart_items"

    cart_item_id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(64), nullable=False, index=True)
    sku_id = Column(String(64), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    unavailable_reason = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class WishlistItemModel(Base):
    """Wishlist/favorites."""
    __tablename__ = "wishlist_items"

    wishlist_item_id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(64), nullable=False, index=True)
    product_id = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Prevent duplicate items in wishlist
        UniqueConstraint('user_id', 'product_id', name='uq_wishlist_user_product'),
    )


class OrderModel(Base):
    """Orders."""
    __tablename__ = "orders"

    order_id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(128), unique=True, nullable=True, index=True)
    status = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.CREATED)
    fulfill_completed = Column(Boolean, nullable=False, default=False, server_default="false")
    total_amount = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    items = relationship("OrderItemModel", back_populates="order", cascade="all, delete-orphan")


class OrderItemModel(Base):
    """Order items — historical snapshot at purchase time."""
    __tablename__ = "order_items"

    order_item_id = Column(String(36), primary_key=True, default=_uuid_str)
    order_id = Column(String(36), ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False)
    sku_id = Column(String(64), nullable=False)
    sku_name = Column(String(255), nullable=False, default="")
    product_id = Column(String(64), nullable=False)
    product_title = Column(String(255), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)

    order = relationship("OrderModel", back_populates="items")


class CollectionModel(Base):
    """Product collections (manual curation)."""
    __tablename__ = "collections"

    collection_id = Column(String(36), primary_key=True, default=_uuid_str)
    title = Column(String(100), nullable=False)
    description = Column(Text)
    product_ids = Column(Text, nullable=False)  # JSON array of product_ids
    max_display = Column(Integer, default=8)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class BannerModel(Base):
    """Promotional banners."""
    __tablename__ = "banners"

    banner_id = Column(String(36), primary_key=True, default=_uuid_str)
    title = Column(String(100), nullable=False)
    image_url = Column(String(500), nullable=False)
    link_url = Column(String(500))
    priority = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class BannerClickModel(Base):
    """Banner click events for CTR analytics."""
    __tablename__ = "banner_clicks"

    click_id = Column(String(36), primary_key=True, default=_uuid_str)
    banner_id = Column(String(36), ForeignKey("banners.banner_id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SubscriptionModel(Base):
    """Product availability change subscriptions.

    One row per (user_id, sku_id). notify_on records the event type
    that triggered the subscription (MVP: only "in_stock").
    """
    __tablename__ = "subscriptions"

    subscription_id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(String(64), nullable=False, index=True)
    sku_id = Column(String(64), nullable=False)
    notify_on = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint('user_id', 'sku_id', name='uq_sub_user_sku'),
    )


class EventIdempotencyKeyModel(Base):
    """Idempotency keys for B2B product events.

    Prevents duplicate processing of events (e.g., stock updates).
    Composite primary key on (event_type, event_id).
    """
    __tablename__ = "event_idempotency_keys"

    event_type = Column(String(64), primary_key=True, nullable=False)
    event_id = Column(String(128), primary_key=True, nullable=False)
    processed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    event_payload = Column(Text, nullable=True, comment='Original event data for debugging/audit')  # JSON string