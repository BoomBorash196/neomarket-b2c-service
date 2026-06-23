"""add subscriptions table

Revision ID: 004_add_subscriptions_table
Revises: 003_add_cancel_pending_status
Create Date: 2026-06-22

Stores user subscriptions for product availability changes.
One row per (user_id, sku_id) — notify_on is stored as a compact
string to keep the MVP simple (see ADR in PR description).
"""

from alembic import op
import sqlalchemy as sa

revision = "004_add_subscriptions_table"
down_revision = "003_add_cancel_pending_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("subscription_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("sku_id", sa.String(64), nullable=False),
        sa.Column("notify_on", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("subscription_id"),
        sa.UniqueConstraint("user_id", "sku_id", name="uq_sub_user_sku"),
    )


def downgrade() -> None:
    op.drop_table("subscriptions")
