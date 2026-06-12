"""encrypt ai_config api_key

Revision ID: c6b4f6a28d4e
Revises: 86da37442254
Create Date: 2026-06-02 13:58:15.681255

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.security.encryption import encrypt_value, decrypt_value


# revision identifiers, used by Alembic.
revision: str = 'c6b4f6a28d4e'
down_revision: Union[str, Sequence[str], None] = '86da37442254'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """加密 ai_configs 表中所有明文 api_key 字段。

    仅处理尚未加密（不以 gAAAAA 开头）的行，跳过已加密行。
    使用 try/except 逐行处理，确保单行失败不影响整体迁移。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # 仅在表存在时执行（test_platform.db 可能没有 ai_configs 表）
    if 'ai_configs' not in inspector.get_table_names():
        return

    ai_configs = sa.table(
        'ai_configs',
        sa.column('id', sa.Integer),
        sa.column('api_key', sa.String),
    )

    rows = conn.execute(sa.select(ai_configs.c.id, ai_configs.c.api_key)).fetchall()
    for row in rows:
        row_id, api_key = row
        if not api_key:
            continue
        # 已加密的跳过
        if api_key.startswith('gAAAAA'):
            continue
        try:
            encrypted = encrypt_value(api_key)
            conn.execute(
                ai_configs.update()
                .where(ai_configs.c.id == row_id)
                .values(api_key=encrypted)
            )
        except Exception as exc:
            # 单行加密失败不阻塞整体迁移，记录警告
            print(f"警告：加密 ai_configs id={row_id} 失败: {exc}")


def downgrade() -> None:
    """解密 ai_configs 表中所有已加密的 api_key 字段，还原为明文。"""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if 'ai_configs' not in inspector.get_table_names():
        return

    ai_configs = sa.table(
        'ai_configs',
        sa.column('id', sa.Integer),
        sa.column('api_key', sa.String),
    )

    rows = conn.execute(sa.select(ai_configs.c.id, ai_configs.c.api_key)).fetchall()
    for row in rows:
        row_id, api_key = row
        if not api_key:
            continue
        # 仅解密已加密的行
        if not api_key.startswith('gAAAAA'):
            continue
        try:
            decrypted = decrypt_value(api_key)
            conn.execute(
                ai_configs.update()
                .where(ai_configs.c.id == row_id)
                .values(api_key=decrypted)
            )
        except Exception as exc:
            print(f"警告：解密 ai_configs id={row_id} 失败: {exc}")