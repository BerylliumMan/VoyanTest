"""add scheduled_tasks missing columns (updated_at, last_run_at, next_run_at)

Revision ID: 2a1b3c4d5e6f
Revises: c6b4f6a28d4e
Create Date: 2026-06-02 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a1b3c4d5e6f'
down_revision: Union[str, Sequence[str], None] = 'c6b4f6a28d4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """检查 SQLite 表中是否存在指定列（跨 DBMS 兼容）。"""
    conn = op.get_bind()
    # SQLite 的 PRAGMA table_info 返回所有列；其它方言可使用 inspector.get_columns
    try:
        rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == column for r in rows)
    except Exception:
        inspector = sa.inspect(conn)
        return any(c['name'] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    """为 uitest.db（旧 schema）补充 scheduled_tasks 缺失列。
    
    test_platform.db 的 scheduled_tasks 已包含这些列（新 create_all 创建），
    通过列存在检查确保幂等。
    """
    # updated_at — 模型有，scheduler_router.py:84,128 写入 → 缺此列会 500
    if not _column_exists('scheduled_tasks', 'updated_at'):
        op.add_column('scheduled_tasks', sa.Column('updated_at', sa.DateTime(), nullable=True))

    # last_run_at — 模型有（旧 DB 用 last_run 列名）
    if not _column_exists('scheduled_tasks', 'last_run_at'):
        op.add_column('scheduled_tasks', sa.Column('last_run_at', sa.DateTime(), nullable=True))

    # next_run_at — 模型有，scheduler_router.py:131 写入（旧 DB 用 next_run 列名）
    if not _column_exists('scheduled_tasks', 'next_run_at'):
        op.add_column('scheduled_tasks', sa.Column('next_run_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """SQLite 不支持 DROP COLUMN（3.35+ 可用但生产慎用），此处跳过回滚。"""
    pass
