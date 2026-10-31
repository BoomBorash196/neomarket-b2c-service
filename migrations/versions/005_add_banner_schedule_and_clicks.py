"""add banner schedule columns and click events table

Revision ID: 005_add_banner_schedule_and_clicks
Revises: 004_add_subscriptions_table
Create Date: 2026-10-31

Adds starts_at/ends_at to banners for schedule-based visibility and a
banner_click_events table for CTR analytics (one row per click).
"""

from alembic import op
import sqlalchemy as sa

revision = "005_add_banner_schedule_and_clicks"
down_revision = "004_add_subscriptions_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("banners", sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("banners", sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "banner_click_events",
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("banner_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_banner_click_events_banner_id", "banner_click_events", ["banner_id"])


def downgrade() -> None:
    op.drop_index("ix_banner_click_events_banner_id", table_name="banner_click_events")
    op.drop_table("banner_click_events")
    op.drop_column("banners", "ends_at")
    op.drop_column("banners", "starts_at")
