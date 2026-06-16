import datetime
import enum
from uuid import uuid4
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Enum as SAEnum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class FileCategory(str, enum.Enum):
    ORIGINAL_VIDEO = "original_video"
    MULTI_VIEW_IMAGE = "multi_view_image"
    INTERMEDIATE_FRAME = "intermediate_frame"
    PLY_MODEL = "ply_model"
    SPLAT_MODEL = "splat_model"
    GLB_MODEL = "glb_model"
    MESH_MODEL = "mesh_model"
    PREVIEW_IMAGE = "preview_image"
    PREVIEW_VIDEO = "preview_video"
    LOG_FILE = "log_file"
    OTHER = "other"


class FileType(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"
    MODEL = "model"
    OTHER = "other"


class MediaProcessingStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class FileDerivativeVariant(str, enum.Enum):
    THUMBNAIL = "thumbnail"
    COMPRESSED = "compressed"
    PREVIEW_VIDEO = "preview_video"


def new_file_id() -> str:
    return f"file_{uuid4().hex}"


class StorageObject(Base):
    __tablename__ = "storage_objects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    pending_delete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("User", back_populates="storage_objects")
    files = relationship("FileRecord", back_populates="storage_object")


class FileRecord(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False, default=new_file_id)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    storage_object_id: Mapped[int] = mapped_column(Integer, ForeignKey("storage_objects.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    original_name: Mapped[str] = mapped_column(String(256), nullable=False)
    category: Mapped[FileCategory] = mapped_column(SAEnum(FileCategory), nullable=False)
    file_type: Mapped[FileType] = mapped_column(SAEnum(FileType), default=FileType.OTHER, nullable=False, index=True)
    mime_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    file_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    metainfo: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), default=dict, nullable=False)
    media_processing_status: Mapped[MediaProcessingStatus] = mapped_column(
        SAEnum(MediaProcessingStatus), default=MediaProcessingStatus.SKIPPED, nullable=False, index=True
    )
    media_processing_error_code: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    media_processing_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    media_processing_task_id: Mapped[str] = mapped_column(String(128), default="", nullable=False, index=True)
    media_processing_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    media_processing_heartbeat_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    media_processed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("User", back_populates="files", foreign_keys=[user_id])
    storage_object = relationship("StorageObject", back_populates="files")
    task_links = relationship("TaskFileRecord", back_populates="file")
    derivatives = relationship(
        "FileDerivativeRecord",
        foreign_keys="FileDerivativeRecord.source_file_id",
        back_populates="source_file",
        cascade="all, delete-orphan",
    )
    source_link = relationship(
        "FileDerivativeRecord",
        foreign_keys="FileDerivativeRecord.derivative_file_id",
        back_populates="derivative_file",
        uselist=False,
        passive_deletes=True,
    )

    @property
    def storage_key(self) -> str:
        return self.public_id


Index(
    "uq_storage_objects_owner_hash_size",
    StorageObject.owner_user_id,
    StorageObject.file_hash,
    StorageObject.file_size,
    unique=True,
)
Index("ix_files_file_hash_file_size", FileRecord.file_hash, FileRecord.file_size)


class FileDerivativeRecord(Base):
    __tablename__ = "file_derivatives"
    __table_args__ = (
        UniqueConstraint("source_file_id", "variant", name="uq_file_derivatives_source_variant"),
        UniqueConstraint("derivative_file_id", name="uq_file_derivatives_derivative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True)
    derivative_file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True)
    variant: Mapped[FileDerivativeVariant] = mapped_column(SAEnum(FileDerivativeVariant), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source_file = relationship("FileRecord", foreign_keys=[source_file_id], back_populates="derivatives")
    derivative_file = relationship("FileRecord", foreign_keys=[derivative_file_id], back_populates="source_link")
