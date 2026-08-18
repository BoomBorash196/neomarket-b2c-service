"""Add unavailable_reason to cart_items and event_idempotency_keys table.

Revision ID: 006
Revises: 005_add_banner_scheduling
Create Date: 2026-06-22

Changes:
- cart_items.unavailable_reason: nullable string
- event_idempotency_keys: idempotency for B2B product events
"""

from alembic import op
import sqlalchemy as sa

revision = '006_add_unavailable_reason_and_event_idempotency'
down_revision = '005_add_banner_scheduling'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'cart_items',
        sa.Column('unavailable_reason', sa.String(64), nullable=True)
    )
    op.create_index(
        'ix_cart_items_sku_id',
        'cart_items',
        ['sku_id'],
        if_not_exists=True
    )

    op.create_table(
        'event_idempotency_keys',
        sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('event_id', sa.String(128), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            'event_payload',
            sa.JSON,
            nullable=True,
            comment='Original event data for debugging/audit'
        ),
        sa.PrimaryKeyConstraint('event_type', 'event_id'),
        comment='Idempotency keys for B2B product events',
    )

    op.create_index(
        'ix_event_idempotency_processed_at',
        'event_idempotency_keys',
        ['processed_at'],
        if_not_exists=True
    )


def downgrade() -> None:
    op.drop_table('event_idempotency_keys', if_exists=True)
    op.drop_index('ix_cart_items_sku_id', 'cart_items', if_exists=True)
    op.drop_column('cart_items', 'unavailable_reason', if_exists=True)