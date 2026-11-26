"""add product identity snapshot to cart items

Revision ID: 008_add_cart_product_identity
Revises: 007_add_cancel_retry_fields
Create Date: 2026-11-26

US-CART-03 requires an unavailable cart line to keep the identifier of the
original product when its SKU disappears from B2B. A product id is not the same
as a variant (SKU) id, so the cart row stores it when the item is added. Only
identity is snapshotted; prices and availability are always resolved live.
"""

from alembic import op
import sqlalchemy as sa

revision = "008_add_cart_product_identity"
down_revision = "007_add_cancel_retry_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cart_items",
        sa.Column("product_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "cart_items",
        sa.Column("product_title", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cart_items", "product_title")
    op.drop_column("cart_items", "product_id")
