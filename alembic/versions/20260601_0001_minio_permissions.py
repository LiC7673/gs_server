"""Rebuild the development schema for DB-backed tasks and MinIO objects.

Revision ID: 20260601_0001
Revises:
Create Date: 2026-06-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260601_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # This is an intentional development reset migration. Existing local test data is discarded.
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())
    for table in ("task_files", "uploads", "files", "storage_objects", "tasks", "users"):
        if table in existing_tables:
            if bind.dialect.name == "postgresql":
                op.execute(sa.text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
            else:
                op.drop_table(table)
    if bind.dialect.name == "postgresql":
        for enum_name in ("taskstatus", "taskvisibility", "taskfilerole", "filecategory", "uploadstatus"):
            op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))

    task_status = sa.Enum(
        "PENDING", "QUEUED", "PROCESSING", "COMPLETED", "PARTIAL_COMPLETED", "FAILED", "CANCELLED", "MANUAL_REVIEW",
        name="taskstatus",
    )
    task_visibility = sa.Enum("PRIVATE", "PUBLIC", name="taskvisibility")
    task_file_role = sa.Enum("INPUT", "RESULT", "PREVIEW", "LOG", "INTERMEDIATE", name="taskfilerole")
    file_category = sa.Enum(
        "ORIGINAL_VIDEO", "MULTI_VIEW_IMAGE", "INTERMEDIATE_FRAME", "PLY_MODEL", "SPLAT_MODEL",
        "GLB_MODEL", "MESH_MODEL", "PREVIEW_IMAGE", "PREVIEW_VIDEO", "LOG_FILE", "OTHER",
        name="filecategory",
    )
    upload_status = sa.Enum("INITIATED", "UPLOADING", "COMPLETED", "CANCELLED", "EXPIRED", name="uploadstatus")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("email", sa.String(128), nullable=False),
        sa.Column("hashed_password", sa.String(256), nullable=False),
        sa.Column("nickname", sa.String(64), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("storage_used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("storage_quota", sa.BigInteger(), nullable=False, server_default=str(50 * 1024 * 1024 * 1024)),
        sa.Column("task_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("task_quota", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("gpu_seconds_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gpu_quota", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "storage_objects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False, unique=True),
        sa.Column("pending_delete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_storage_objects_owner_user_id", "storage_objects", ["owner_user_id"])
    op.create_index("ix_storage_objects_pending_delete", "storage_objects", ["pending_delete"])
    op.create_index(
        "uq_storage_objects_owner_hash_size",
        "storage_objects",
        ["owner_user_id", "file_hash", "file_size"],
        unique=True,
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("algorithm", sa.String(64), nullable=False),
        sa.Column("params", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("visibility", task_visibility, nullable=False, server_default="PRIVATE"),
        sa.Column("status", task_status, nullable=False, server_default="PENDING"),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("current_stage", sa.String(128), nullable=False, server_default=""),
        sa.Column("input_kind", sa.String(32), nullable=False, server_default=""),
        sa.Column("error_code", sa.String(128), nullable=False, server_default=""),
        sa.Column("error_status_code", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("stdout_tail", sa.Text(), nullable=False, server_default=""),
        sa.Column("stderr_tail", sa.Text(), nullable=False, server_default=""),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("celery_task_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("process_id", sa.Integer()),
        sa.Column("worker_node_id", sa.String(128)),
        sa.Column("executor_id", sa.String(128)),
        sa.Column("cuda_device", sa.String(32)),
        sa.Column("execution_attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("gpu_seconds_cost", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    for column in ("public_id", "user_id", "visibility", "status", "celery_task_id", "worker_node_id", "executor_id", "cuda_device", "heartbeat_at", "is_deleted"):
        op.create_index(f"ix_tasks_{column}", "tasks", [column], unique=column == "public_id")

    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("storage_object_id", sa.Integer(), sa.ForeignKey("storage_objects.id"), nullable=False),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("original_name", sa.String(256), nullable=False),
        sa.Column("category", file_category, nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False, server_default="application/octet-stream"),
        sa.Column("file_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("file_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    for column in ("public_id", "user_id", "storage_object_id", "file_hash", "is_deleted"):
        op.create_index(f"ix_files_{column}", "files", [column], unique=column == "public_id")
    op.create_index("ix_files_file_hash_file_size", "files", ["file_hash", "file_size"])

    op.create_table(
        "task_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", task_file_role, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "file_id", "role", name="uq_task_files_task_file_role"),
    )
    op.create_index("ix_task_files_role", "task_files", ["role"])
    op.create_index("ix_task_files_task_role", "task_files", ["task_id", "role"])

    op.create_table(
        "uploads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("upload_id", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("files.id")),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False, server_default="application/octet-stream"),
        sa.Column("file_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False),
        sa.Column("total_chunks", sa.Integer(), nullable=False),
        sa.Column("received_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", upload_status, nullable=False, server_default="INITIATED"),
        sa.Column("uploaded_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expired_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_uploads_upload_id", "uploads", ["upload_id"], unique=True)
    op.create_index("ix_uploads_user_id", "uploads", ["user_id"])


def downgrade() -> None:
    for table in ("uploads", "task_files", "files", "tasks", "storage_objects", "users"):
        op.drop_table(table)
