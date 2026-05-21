"""Initial schema.

Revision ID: 001
Revises: 
Create Date: 2026-05-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Cart
    op.create_table(
        'cart_items',
        sa.Column('cart_item_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('sku_id', sa.String(length=64), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False, default=1),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.PrimaryKeyConstraint('cart_item_id')
    )
    op.create_index('ix_cart_items_user_id', 'cart_items', ['user_id'])

    # Wishlist
    op.create_table(
        'wishlist_items',
        sa.Column('wishlist_item_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('product_id', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('wishlist_item_id'),
        sa.UniqueConstraint('user_id', 'product_id', name='uq_wishlist_user_product')
    )
    op.create_index('ix_wishlist_items_user_id', 'wishlist_items', ['user_id'])

    # Orders
    op.create_table(
        'orders',
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('status', sa.Enum('CREATED', 'PAID', 'ASSEMBLING', 'DELIVERING', 'DELIVERED', 'CANCELLED', name='orderstatus'), nullable=False),
        sa.Column('total_amount', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
        sa.PrimaryKeyConstraint('order_id')
    )
    op.create_index('ix_orders_user_id', 'orders', ['user_id'])

    # Order Items
    op.create_table(
        'order_items',
        sa.Column('order_item_id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('sku_id', sa.String(length=64), nullable=False),
        sa.Column('product_id', sa.String(length=64), nullable=False),
        sa.Column('product_title', sa.String(length=255), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('price', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(['order_id'], ['orders.order_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('order_item_id')
    )

    # Collections
    op.create_table(
        'collections',
        sa.Column('collection_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('product_ids', sa.Text(), nullable=False),
        sa.Column('max_display', sa.Integer(), default=8),
        sa.Column('is_active', sa.Boolean(), default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('collection_id')
    )

    # Banners
    op.create_table(
        'banners',
        sa.Column('banner_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=100), nullable=False),
        sa.Column('image_url', sa.String(length=500), nullable=False),
        sa.Column('link_url', sa.String(length=500)),
        sa.Column('priority', sa.Integer(), default=0),
        sa.Column('is_active', sa.Boolean(), default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('banner_id')
    )


def downgrade() -> None:
    op.drop_table('banners')
    op.drop_table('collections')
    op.drop_table('order_items')
    op.drop_table('orders')
    op.drop_table('wishlist_items')
    op.drop_table('cart_items')
