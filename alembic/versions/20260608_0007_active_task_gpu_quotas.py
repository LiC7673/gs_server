"""Add active-task and GPU quota accounting fields.

Revision ID: 20260608_0007
Revises: 20260607_0006
Create Date: 2026-06-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260608_0007"
down_revision: Union[str, None] = "20260607_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("gpu_concurrency_quota", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("users", sa.Column("gpu_usage_date", sa.Date(), nullable=True))
    op.add_column("tasks", sa.Column("gpu_billing_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("queue_reason", sa.String(length=64), nullable=False, server_default=""))
    op.create_index(
        "ix_tasks_user_status_deleted",
        "tasks",
        ["user_id", "status", "is_deleted"],
        unique=False,
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "UPDATE users "
                "SET gpu_usage_date = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date "
                "WHERE gpu_usage_date IS NULL"
            )
        )
    else:
        op.execute(sa.text("UPDATE users SET gpu_usage_date = CURRENT_DATE WHERE gpu_usage_date IS NULL"))


def downgrade() -> None:
    op.drop_index("ix_tasks_user_status_deleted", table_name="tasks")
    op.drop_column("tasks", "queue_reason")
    op.drop_column("tasks", "gpu_billing_started_at")
    op.drop_column("users", "gpu_usage_date")
    op.drop_column("users", "gpu_concurrency_quota")
