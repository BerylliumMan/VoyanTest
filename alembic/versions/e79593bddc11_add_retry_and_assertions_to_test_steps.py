"""add_retry_and_assertions_to_test_steps

Revision ID: e79593bddc11
Revises: 6e7f8a9b0c1d
Create Date: 2026-06-16 11:28:28.497593

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e79593bddc11'
down_revision: Union[str, Sequence[str], None] = '6e7f8a9b0c1d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('test_steps', sa.Column('retry_max', sa.Integer(), nullable=True))
    op.add_column('test_steps', sa.Column('retry_delay', sa.Float(), nullable=True))
    op.add_column('test_steps', sa.Column('assertions', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # SQLite 不支持 DROP COLUMN，降级时不做操作
    pass
