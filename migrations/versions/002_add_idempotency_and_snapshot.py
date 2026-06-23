"""Add idempotency_key, sku_name, unit_price.

Revision ID: 002
Revises: 001
Create Date: 2026-06-18

Changes:
- orders: add idempotency_key (unique, nullable) for checkout idempotency
- order_items: add sku_name, rename price → unit_price, add product_id
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # orders: add idempotency_key
    op.add_column('orders',
        sa.Column('idempotency_key', sa.String(length=128), nullable=True)
    )
    op.create_index('ix_orders_idempotency_key', 'orders', ['idempotency_key'])
    op.create_unique_constraint('uq_orders_idempotency_key', 'orders', ['idempotency_key'])

    # order_items: add sku_name
    op.add_column('order_items',
        sa.Column('sku_name', sa.String(length=255), nullable=False, server_default='')
    )

    # order_items: rename price → unit_price
    op.add_column('order_items',
        sa.Column('unit_price', sa.Float(), nullable=False)
    )
    op.execute('UPDATE order_items SET unit_price = price')


def downgrade() -> None:
    op.drop_column('order_items', 'unit_price')
    op.drop_column('order_items', 'sku_name')
    op.drop_constraint('uq_orders_idempotency_key', 'orders', type_='unique')
    op.drop_index('ix_orders_idempotency_key', table_name='orders')
    op.drop_column('orders', 'idempotency_key')
