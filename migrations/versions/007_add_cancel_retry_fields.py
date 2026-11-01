"""add cancel retry metadata to orders

Revision ID: 007_add_cancel_retry_fields
Revises: 006_add_order_checkout_fields
Create Date: 2026-11-01

US-ORD-03 accepts the cancellation intent by moving the order to
CANCEL_PENDING before calling B2B. If unreserve fails, the retry worker needs
the attempt count, the next backoff timestamp and the last error.
"""

from alembic import op
import sqlalchemy as sa

revision = "007_add_cancel_retry_fields"
down_revision = "006_add_order_checkout_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "cancel_retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "cancel_next_retry_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "orders",
        sa.Column("cancel_last_error", sa.String(500), nullable=True),
    )
    op.create_index(
        "ix_orders_cancel_retry",
        "orders",
        ["status", "cancel_next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_orders_cancel_retry", table_name="orders")
    op.drop_column("orders", "cancel_last_error")
    op.drop_column("orders", "cancel_next_retry_at")
    op.drop_column("orders", "cancel_retry_count")
