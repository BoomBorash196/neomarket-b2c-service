"""Add fulfill_completed flag to orders.

Revision ID: 007_add_fulfill_completed
Revises: 006_add_unavailable_reason_and_event_idempotency
Create Date: 2026-06-23

Tracks whether B2B fulfill succeeded for a DELIVERED order.
"""

from alembic import op
import sqlalchemy as sa

revision = "007_add_fulfill_completed"
down_revision = "006_add_unavailable_reason_and_event_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("fulfill_completed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("orders", "fulfill_completed")