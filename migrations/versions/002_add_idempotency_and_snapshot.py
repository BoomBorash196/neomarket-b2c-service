"""Add idempotency_key, sku_name, unit_price.

Revision ID: 002
Revises: 001
Create Date: 2026-06-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders',
        sa.Column('idempotency_key', sa.String(length=128), nullable=True)
    )
    op.create_index('ix_orders_idempotency_key', 'orders', ['idempotency_key'])
    op.create_unique_constraint('uq_orders_idempotency_key', 'orders', ['idempotency_key'])


def downgrade() -> None:
    op.drop_constraint('uq_orders_idempotency_key', 'orders', type_='unique')
    op.drop_index('ix_orders_idempotency_key', table_name='orders')
    op.drop_column('orders', 'idempotency_key')