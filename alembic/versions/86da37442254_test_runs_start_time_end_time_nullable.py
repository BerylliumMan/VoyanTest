"""test_runs start_time end_time nullable

Revision ID: 86da37442254
Revises: 0044390972f6
Create Date: 2026-06-02 11:36:09.330503

让 test_runs.start_time / end_time 可空，根治"预创建 pending 行被卡死检查误判为 failed"的 bug。
预创建占位时 start_time=None，runner 实际启动用例时再写入。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '86da37442254'
down_revision: Union[str, Sequence[str], None] = '0044390972f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('test_runs') as batch_op:
        batch_op.alter_column('start_time', existing_type=sa.DATETIME(), nullable=True)
        batch_op.alter_column('end_time', existing_type=sa.DATETIME(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('test_runs') as batch_op:
        batch_op.alter_column('end_time', existing_type=sa.DATETIME(), nullable=False)
        batch_op.alter_column('start_time', existing_type=sa.DATETIME(), nullable=False)
