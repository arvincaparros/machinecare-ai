"""users, roles, refresh sessions, reset tokens

Revision ID: e5a7c1f90b34
Revises: 6041a26eff31
Create Date: 2026-11-07 10:12:00.000000

Creates the authentication schema for Phase 1 and seeds the three
fixed roles. No default admin is created here on purpose: the first
administrator comes from the one-time CLI
(``python -m app.scripts.create_admin``), never from a migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5a7c1f90b34'
down_revision: Union[str, Sequence[str], None] = '6041a26eff31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Seed data for the fixed role set (names match app.core.permissions).
_roles_seed = [
    {"id": 1, "name": "admin",
     "description": "Full control: users, imports, reindex, audit."},
    {"id": 2, "name": "maintenance_engineer",
     "description": "Uses the assistant and runs incident imports."},
    {"id": 3, "name": "viewer",
     "description": "Uses the assistant and reads incident answers."},
]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('users',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('display_name', sa.String(length=150), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('token_version', sa.Integer(), nullable=False),
    sa.Column('failed_login_count', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True),
              server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True),
              server_default=sa.text('now()'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.BigInteger(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('username')
    )
    op.create_table('roles',
    sa.Column('id', sa.SmallInteger(), autoincrement=True, nullable=False),
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('user_roles',
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('role_id', sa.SmallInteger(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                            ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'role_id')
    )
    op.create_table('refresh_sessions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('rotated_from', sa.Uuid(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('user_agent', sa.String(length=255), nullable=True),
    sa.Column('ip_hash', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                            ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_table('password_reset_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                            ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )

    roles_table = sa.table(
        'roles',
        sa.column('id', sa.SmallInteger),
        sa.column('name', sa.String),
        sa.column('description', sa.String),
    )
    op.bulk_insert(roles_table, _roles_seed)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('password_reset_tokens')
    op.drop_table('refresh_sessions')
    op.drop_table('user_roles')
    op.drop_table('roles')
    op.drop_table('users')
