"""audit_log

Revision ID: b2d4f6a80c15
Revises: e5a7c1f90b34
Create Date: 2026-11-07 10:14:00.000000

Creates the append-only audit log used from Phase 1 for authentication
and user-administration events. Metadata is JSONB on PostgreSQL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'b2d4f6a80c15'
down_revision: Union[str, Sequence[str], None] = 'e5a7c1f90b34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('audit_log',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True),
              server_default=sa.text('now()'), nullable=False),
    sa.Column('actor_user_id', sa.BigInteger(), nullable=True),
    sa.Column('actor_username', sa.String(length=64), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('resource_type', sa.String(length=64), nullable=True),
    sa.Column('resource_id', sa.String(length=128), nullable=True),
    sa.Column('request_id', sa.String(length=36), nullable=True),
    sa.Column('ip_hash', sa.String(length=64), nullable=True),
    sa.Column('metadata', JSONB(), server_default=sa.text("'{}'::jsonb"),
              nullable=False),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('audit_log')
