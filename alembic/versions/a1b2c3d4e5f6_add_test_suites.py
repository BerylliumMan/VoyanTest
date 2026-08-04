"""add test_suites and test_suite_cases

Revision ID: a1b2c3d4e5f6
Revises: 948168709616
Create Date: 2026-08-04 08:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "948168709616"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "test_suites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("case_kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_test_suites_id"), "test_suites", ["id"], unique=False)
    op.create_index(op.f("ix_test_suites_project_id"), "test_suites", ["project_id"], unique=False)
    op.create_index(op.f("ix_test_suites_case_kind"), "test_suites", ["case_kind"], unique=False)

    op.create_table(
        "test_suite_cases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("suite_id", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["test_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["suite_id"], ["test_suites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("suite_id", "case_id", name="uq_suite_case"),
    )
    op.create_index(op.f("ix_test_suite_cases_id"), "test_suite_cases", ["id"], unique=False)
    op.create_index(op.f("ix_test_suite_cases_suite_id"), "test_suite_cases", ["suite_id"], unique=False)
    op.create_index(op.f("ix_test_suite_cases_case_id"), "test_suite_cases", ["case_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_test_suite_cases_case_id"), table_name="test_suite_cases")
    op.drop_index(op.f("ix_test_suite_cases_suite_id"), table_name="test_suite_cases")
    op.drop_index(op.f("ix_test_suite_cases_id"), table_name="test_suite_cases")
    op.drop_table("test_suite_cases")
    op.drop_index(op.f("ix_test_suites_case_kind"), table_name="test_suites")
    op.drop_index(op.f("ix_test_suites_project_id"), table_name="test_suites")
    op.drop_index(op.f("ix_test_suites_id"), table_name="test_suites")
    op.drop_table("test_suites")
