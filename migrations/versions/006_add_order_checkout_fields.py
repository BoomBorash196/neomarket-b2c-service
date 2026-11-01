"""add checkout snapshot and idempotency fingerprint to orders

Revision ID: 006_add_order_checkout_fields
Revises: 005_add_banner_schedule_and_clicks
Create Date: 2026-11-01

US-ORD-01 checkout needs a place to keep the address/payment references,
the request fingerprint used to detect Idempotency-Key reuse, and the
delivery/cancel metadata that is returned in the OpenAPI OrderResponse.
"""

from alembic import op
import sqlalchemy as sa

revision = "006_add_order_checkout_fields"
down_revision = "005_add_banner_schedule_and_clicks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("request_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column(
            "delivery_cost",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "orders",
        sa.Column("address_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("payment_method_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("comment", sa.Text(), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("cancel_reason", sa.String(500), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "paid_at")
    op.drop_column("orders", "cancel_reason")
    op.drop_column("orders", "comment")
    op.drop_column("orders", "payment_method_id")
    op.drop_column("orders", "address_id")
    op.drop_column("orders", "delivery_cost")
    op.drop_column("orders", "request_hash")
