"""Add user avatar file binding.

Revision ID: 20260601_0004
Revises: 20260601_0003
Create Date: 2026-06-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260601_0004"
down_revision: Union[str, None] = "20260601_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_file_record_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_users_avatar_file_record_id_files",
        "users",
        "files",
        ["avatar_file_record_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_users_avatar_file_record_id", "users", ["avatar_file_record_id"])


def downgrade() -> None:
    op.drop_index("ix_users_avatar_file_record_id", table_name="users")
    op.drop_constraint("fk_users_avatar_file_record_id_files", "users", type_="foreignkey")
    op.drop_column("users", "avatar_file_record_id")
