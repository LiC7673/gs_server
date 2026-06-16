"""Make the selected Mesh algorithm nullable.

Revision ID: 20260607_0006
Revises: 20260607_0005
Create Date: 2026-06-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260607_0006"
down_revision: Union[str, None] = "20260607_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("tasks", "mesh_algorithm", existing_type=sa.String(64), nullable=True, server_default=None)
    op.execute(
        sa.text(
            "UPDATE tasks SET mesh_algorithm = NULL "
            "WHERE mesh_algorithm = 'dash_gaussian_mesh' "
            "AND algorithm NOT IN ('dash_gaussian_mesh', 'hunyuan3d') "
            "AND current_stage NOT IN ('mesh_queued', 'mesh_processing', 'mesh_completed', 'mesh_failed') "
            "AND COALESCE(mesh_params, '{}') = '{}'"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("UPDATE tasks SET mesh_algorithm = 'dash_gaussian_mesh' WHERE mesh_algorithm IS NULL"))
    op.alter_column(
        "tasks",
        "mesh_algorithm",
        existing_type=sa.String(64),
        nullable=False,
        server_default="dash_gaussian_mesh",
    )
