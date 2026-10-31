"""Ensure CANCEL_PENDING exists in the order status enum.

Revision ID: 003_add_cancel_pending_status
Revises: 002
Create Date: 2026-06-18
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '003_add_cancel_pending_status'
down_revision: Union[str, None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL native enum; SQLite stores enums as VARCHAR and needs nothing.
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'CANCEL_PENDING'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values directly. Recreate the
    # type without CANCEL_PENDING (only meaningful on PostgreSQL).
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute("DROP TYPE IF EXISTS orderstatus_new CASCADE")
    op.execute(
        "CREATE TYPE orderstatus_new AS ENUM "
        "('CREATED', 'PAID', 'ASSEMBLING', 'DELIVERING', 'DELIVERED', 'CANCELLED')"
    )
    op.execute(
        "ALTER TABLE orders ALTER COLUMN status TYPE orderstatus_new "
        "USING status::text::orderstatus_new"
    )
    op.execute("DROP TYPE orderstatus")
    op.execute("ALTER TYPE orderstatus_new RENAME TO orderstatus")
