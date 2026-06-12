"""add cookies column to environments

Revision ID: 6e7f8a9b0c1d
Revises: 5083d482d549
Create Date: 2026-06-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6e7f8a9b0c1d'
down_revision: Union[str, Sequence[str], None] = '5083d482d549'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """检查 SQLite 表中是否存在指定列（幂等迁移）。"""
    conn = op.get_bind()
    try:
        rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == column for r in rows)
    except Exception:
        inspector = sa.inspect(conn)
        return any(c['name'] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    """为 environments 表添加 cookies JSON 列，用于存储预置 cookie 列表。"""
    if not _column_exists('environments', 'cookies'):
        with op.batch_alter_table('environments') as batch_op:
            batch_op.add_column(
                sa.Column('cookies', sa.JSON(), nullable=True)
            )


def downgrade() -> None:
    """SQLite 不支持 DROP COLUMN（3.35+ 可用但生产慎用），此处跳过回滚。"""
    pass
