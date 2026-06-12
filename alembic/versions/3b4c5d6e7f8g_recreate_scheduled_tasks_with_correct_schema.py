"""recreate scheduled_tasks with correct schema (INTEGER PK, not VARCHAR)

Revision ID: 3b4c5d6e7f8g
Revises: 2a1b3c4d5e6f
Create Date: 2026-06-02 17:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3b4c5d6e7f8g'
down_revision: Union[str, Sequence[str], None] = '2a1b3c4d5e6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop broken scheduled_tasks table (VARCHAR id, old columns) and recreate with correct schema.

    uitest.db has scheduled_tasks with id=VARCHAR PK which breaks autoincrement.
    Both scheduled_tasks and scheduled_task_runs have 0 rows — safe to drop & recreate.
    test_platform.db already has correct schema — skip if id is already INTEGER.
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if 'scheduled_tasks' not in inspector.get_table_names():
        return

    # Check if id column is already INTEGER (correct) — skip if so
    cols = inspector.get_columns('scheduled_tasks')
    id_col = next((c for c in cols if c['name'] == 'id'), None)
    if id_col and str(id_col['type']) in ('INTEGER', 'BIGINT'):
        return  # Schema is already correct

    # Drop scheduled_task_runs first (FK dependency, but no data)
    if 'scheduled_task_runs' in inspector.get_table_names():
        op.drop_table('scheduled_task_runs')

    # Drop old broken scheduled_tasks
    op.drop_table('scheduled_tasks')

    # Recreate with correct schema matching db_models.ScheduledTask
    op.create_table(
        'scheduled_tasks',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=True),
        sa.Column('cron_expression', sa.String(100), nullable=False),
        sa.Column('task_type', sa.String(50), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default='1', nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(), nullable=True),
        sa.Column('run_count', sa.Integer(), server_default='0', nullable=True),
    )

    # Recreate scheduled_task_runs
    op.create_table(
        'scheduled_task_runs',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('start_time', sa.DateTime(), nullable=False),
        sa.Column('end_time', sa.DateTime(), nullable=True),
        sa.Column('duration', sa.Float(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Cannot safely recreate old broken schema — skip downgrade."""
    pass
