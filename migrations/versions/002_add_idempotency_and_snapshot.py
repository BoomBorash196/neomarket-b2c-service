"""Add idempotency_key to orders.

Revision ID: 002
Revises: 001
Create Date: 2026-06-18

The old revision also tried to add ``order_items.sku_name`` / ``unit_price``
and never dropped the legacy ``price`` column, which left a NOT NULL column
without a default that ORM inserts could not fill. The initial schema now
creates the final snapshot columns, so only the checkout idempotency key
remains here.
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
    op.add_column('orders', sa.Column('idempotency_key', sa.String(length=128), nullable=True))
    # A unique index works on both PostgreSQL and SQLite (no table rebuild).
    op.create_index(
        'ix_orders_idempotency_key', 'orders', ['idempotency_key'], unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_orders_idempotency_key', table_name='orders')
    op.drop_column('orders', 'idempotency_key')
