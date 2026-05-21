"""SQLAlchemy ORM models."""

from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from src.database import Base
from src.schemas import OrderStatus


class CartItemModel(Base):
    """Shopping cart items."""
    __tablename__ = "cart_items"

    cart_item_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), nullable=False, index=True)
    sku_id = Column(String(64), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class WishlistItemModel(Base):
    """Wishlist/favorites."""
    __tablename__ = "wishlist_items"

    wishlist_item_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), nullable=False, index=True)
    product_id = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Prevent duplicate items in wishlist
        {"unique": True, "sqlite_autoincrement": True},
    )


class OrderModel(Base):
    """Orders."""
    __tablename__ = "orders"

    order_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), nullable=False, index=True)
    status = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.CREATED)
    total_amount = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    items = relationship("OrderItemModel", back_populates="order", cascade="all, delete-orphan")


class OrderItemModel(Base):
    """Order items."""
    __tablename__ = "order_items"

    order_item_id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False)
    sku_id = Column(String(64), nullable=False)
    product_id = Column(String(64), nullable=False)
    product_title = Column(String(255), nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)

    order = relationship("OrderModel", back_populates="items")


class CollectionModel(Base):
    """Product collections (manual curation)."""
    __tablename__ = "collections"

    collection_id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(100), nullable=False)
    description = Column(Text)
    product_ids = Column(Text, nullable=False)  # JSON array of product_ids
    max_display = Column(Integer, default=8)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class BannerModel(Base):
    """Promotional banners."""
    __tablename__ = "banners"

    banner_id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(100), nullable=False)
    image_url = Column(String(500), nullable=False)
    link_url = Column(String(500))
    priority = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
