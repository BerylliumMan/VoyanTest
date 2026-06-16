"""add_project_ids_to_users

Revision ID: f6a477530b1e
Revises: e79593bddc11
Create Date: 2026-06-16 12:44:17.186056

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a477530b1e'
down_revision: Union[str, Sequence[str], None] = 'e79593bddc11'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('project_ids', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'project_ids')
