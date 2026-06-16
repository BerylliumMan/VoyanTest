"""add_healed_selector_to_test_steps

Revision ID: 502ebc828691
Revises: f6a477530b1e
Create Date: 2026-06-16 13:52:07.518496

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '502ebc828691'
down_revision: Union[str, Sequence[str], None] = 'f6a477530b1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('test_steps', sa.Column('healed_selector', sa.String(length=500), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('test_steps', 'healed_selector')
