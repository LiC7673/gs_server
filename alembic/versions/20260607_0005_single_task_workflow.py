"""Add single-task reconstruction workflow fields.

Revision ID: 20260607_0005
Revises: 20260601_0004
Create Date: 2026-06-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260607_0005"
down_revision: Union[str, None] = "20260601_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("gaussian_algorithm", sa.String(64), nullable=False, server_default=""))
    op.add_column("tasks", sa.Column("gaussian_params", sa.Text(), nullable=False, server_default="{}"))
    op.add_column(
        "tasks",
        sa.Column("mesh_algorithm", sa.String(64), nullable=False, server_default="dash_gaussian_mesh"),
    )
    op.add_column("tasks", sa.Column("mesh_params", sa.Text(), nullable=False, server_default="{}"))
    op.execute(
        sa.text(
            "UPDATE tasks SET gaussian_algorithm = algorithm, gaussian_params = params "
            "WHERE gaussian_algorithm = ''"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tasks SET current_stage = 'task_created' "
            "WHERE status = 'PENDING' AND current_stage = ''"
        )
    )

    op.add_column("uploads", sa.Column("task_record_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_uploads_task_record_id_tasks",
        "uploads",
        "tasks",
        ["task_record_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_uploads_task_record_id", "uploads", ["task_record_id"])


def downgrade() -> None:
    op.drop_index("ix_uploads_task_record_id", table_name="uploads")
    op.drop_constraint("fk_uploads_task_record_id_tasks", "uploads", type_="foreignkey")
    op.drop_column("uploads", "task_record_id")
    for column in ("mesh_params", "mesh_algorithm", "gaussian_params", "gaussian_algorithm"):
        op.drop_column("tasks", column)
