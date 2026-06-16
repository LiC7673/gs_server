"""Add media metadata and file derivatives.

Revision ID: 20260601_0002
Revises: 20260601_0001
Create Date: 2026-06-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260601_0002"
down_revision: Union[str, None] = "20260601_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    file_type = sa.Enum("IMAGE", "VIDEO", "MODEL", "OTHER", name="filetype")
    media_status = sa.Enum("PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED", name="mediaprocessingstatus")
    derivative_variant = sa.Enum("THUMBNAIL", "COMPRESSED", "PREVIEW_VIDEO", name="filederivativevariant")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        file_type.create(bind, checkfirst=True)
        media_status.create(bind, checkfirst=True)

    op.add_column("files", sa.Column("file_type", file_type, nullable=False, server_default="OTHER"))
    op.add_column(
        "files",
        sa.Column(
            "metainfo",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "files",
        sa.Column("media_processing_status", media_status, nullable=False, server_default="SKIPPED"),
    )
    op.add_column("files", sa.Column("media_processing_error_code", sa.String(128), nullable=False, server_default=""))
    op.add_column("files", sa.Column("media_processing_error", sa.Text(), nullable=False, server_default=""))
    op.add_column("files", sa.Column("media_processing_task_id", sa.String(128), nullable=False, server_default=""))
    op.add_column("files", sa.Column("media_processing_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("files", sa.Column("media_processing_heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("files", sa.Column("media_processed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_files_file_type", "files", ["file_type"])
    op.create_index("ix_files_media_processing_status", "files", ["media_processing_status"])
    op.create_index("ix_files_media_processing_task_id", "files", ["media_processing_task_id"])
    op.create_index("ix_files_media_processing_heartbeat_at", "files", ["media_processing_heartbeat_at"])
    op.execute(sa.text("UPDATE files SET file_type = 'IMAGE' WHERE mime_type LIKE 'image/%'"))
    op.execute(sa.text("UPDATE files SET file_type = 'VIDEO' WHERE mime_type LIKE 'video/%'"))
    op.execute(
        sa.text(
            "UPDATE files SET file_type = 'MODEL' "
            "WHERE mime_type LIKE 'model/%' OR category IN ('PLY_MODEL', 'SPLAT_MODEL', 'GLB_MODEL')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE files SET media_processing_status = 'PENDING' "
            "WHERE category IN ('MULTI_VIEW_IMAGE', 'ORIGINAL_VIDEO')"
        )
    )

    op.create_table(
        "file_derivatives",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("derivative_file_id", sa.Integer(), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("variant", derivative_variant, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("source_file_id", "variant", name="uq_file_derivatives_source_variant"),
        sa.UniqueConstraint("derivative_file_id", name="uq_file_derivatives_derivative"),
    )
    op.create_index("ix_file_derivatives_source_file_id", "file_derivatives", ["source_file_id"])
    op.create_index("ix_file_derivatives_derivative_file_id", "file_derivatives", ["derivative_file_id"])


def downgrade() -> None:
    op.drop_table("file_derivatives")
    for index in (
        "ix_files_media_processing_heartbeat_at",
        "ix_files_media_processing_task_id",
        "ix_files_media_processing_status",
        "ix_files_file_type",
    ):
        op.drop_index(index, table_name="files")
    for column in (
        "media_processed_at",
        "media_processing_heartbeat_at",
        "media_processing_attempts",
        "media_processing_task_id",
        "media_processing_error",
        "media_processing_error_code",
        "media_processing_status",
        "metainfo",
        "file_type",
    ):
        op.drop_column("files", column)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for enum_name in ("filederivativevariant", "mediaprocessingstatus", "filetype"):
            op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))
