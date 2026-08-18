"""add banner scheduling and banner_clicks table

Revision ID: 005_add_banner_scheduling
Revises: 004_add_subscriptions_table
Create Date: 2026-06-22
"""

from alembic import op
import sqlalchemy as sa

revision = "005_add_banner_scheduling"
down_revision = "004_add_subscriptions_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("banners", sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("banners", sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "banner_clicks",
        sa.Column("click_id", sa.String(36), nullable=False),
        sa.Column("banner_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("click_id"),
        sa.ForeignKeyConstraint(
            ["banner_id"],
            ["banners.banner_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_banner_clicks_banner_id", "banner_clicks", ["banner_id"])


def downgrade() -> None:
    op.drop_table("banner_clicks")
    op.drop_column("banners", "ends_at")
    op.drop_column("banners", "starts_at")