"""Add CANCEL_PENDING to order status enum.

Revision ID: 003
Revises: 002
Create Date: 2026-06-18

Changes:
- Add CANCEL_PENDING value to orderstatus enum in PostgreSQL
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '003'
down_revision: Union[str, None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add CANCEL_PENDING to the PostgreSQL enum type
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'CANCEL_PENDING'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values directly.
    # Workaround: recreate the enum type without CANCEL_PENDING.
    op.execute("DROP TYPE IF EXISTS orderstatus_new CASCADE")
    op.execute("CREATE TYPE orderstatus_new AS ENUM ('CREATED', 'PAID', 'ASSEMBLING', 'DELIVERING', 'DELIVERED', 'CANCELLED')")
    op.execute("ALTER TABLE orders ALTER COLUMN status TYPE orderstatus_new USING status::text::orderstatus_new")
    op.execute("DROP TYPE orderstatus")
    op.execute("ALTER TYPE orderstatus_new RENAME TO orderstatus")
