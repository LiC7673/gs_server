"""Add mesh file category and partial completed task status.

Revision ID: 20260601_0003
Revises: 20260601_0002
Create Date: 2026-06-05
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260601_0003"
down_revision: Union[str, None] = "20260601_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_postgres_enum_value(enum_name: str, value: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _add_postgres_enum_value("taskstatus", "PARTIAL_COMPLETED")
        _add_postgres_enum_value("filecategory", "MESH_MODEL")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely without rebuilding dependent columns.
    pass
