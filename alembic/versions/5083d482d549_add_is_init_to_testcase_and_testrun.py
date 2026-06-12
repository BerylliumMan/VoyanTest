"""add_is_init_to_testcase_and_testrun

Revision ID: 5083d482d549
Revises: 3b4c5d6e7f8g
Create Date: 2026-06-08 16:05:57.804779

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5083d482d549'
down_revision: Union[str, Sequence[str], None] = '3b4c5d6e7f8g'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # sqlite batch mode
    with op.batch_alter_table('test_cases') as batch_op:
        batch_op.add_column(sa.Column('is_init', sa.Boolean(), server_default=sa.text('0'), nullable=False))

    with op.batch_alter_table('test_runs') as batch_op:
        batch_op.add_column(sa.Column('is_init', sa.Boolean(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('test_cases') as batch_op:
        batch_op.drop_column('is_init')

    with op.batch_alter_table('test_runs') as batch_op:
        batch_op.drop_column('is_init')
