import datetime
import enum
from uuid import uuid4
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIAL_COMPLETED = "partial_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MANUAL_REVIEW = "manual_review"


class TaskVisibility(str, enum.Enum):
    PRIVATE = "private"
    PUBLIC = "public"


class TaskFileRole(str, enum.Enum):
    INPUT = "input"
    RESULT = "result"
    PREVIEW = "preview"
    LOG = "log"
    INTERMEDIATE = "intermediate"


def new_task_id() -> str:
    return f"recon_{uuid4().hex}"


class TaskRecord(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False, default=new_task_id)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[str] = mapped_column(Text, default="{}")
    gaussian_algorithm: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    gaussian_params: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    mesh_algorithm: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    mesh_params: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    visibility: Mapped[TaskVisibility] = mapped_column(
        SAEnum(TaskVisibility), default=TaskVisibility.PRIVATE, nullable=False, index=True
    )
    status: Mapped[TaskStatus] = mapped_column(SAEnum(TaskStatus), default=TaskStatus.PENDING, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    current_stage: Mapped[str] = mapped_column(String(128), default="")
    input_kind: Mapped[str] = mapped_column(String(32), default="")
    error_code: Mapped[str] = mapped_column(String(128), default="")
    error_status_code: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    stdout_tail: Mapped[str] = mapped_column(Text, default="")
    stderr_tail: Mapped[str] = mapped_column(Text, default="")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    celery_task_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    process_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    worker_node_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    executor_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    cuda_device: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    execution_attempt: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    gpu_seconds_cost: Mapped[int] = mapped_column(Integer, default=0)
    gpu_billing_started_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    queue_reason: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    heartbeat_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner = relationship("User", back_populates="tasks")
    file_links = relationship("TaskFileRecord", back_populates="task", cascade="all, delete-orphan")


class TaskFileRecord(Base):
    __tablename__ = "task_files"
    __table_args__ = (
        UniqueConstraint("task_id", "file_id", "role", name="uq_task_files_task_file_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[TaskFileRole] = mapped_column(SAEnum(TaskFileRole), nullable=False, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task = relationship("TaskRecord", back_populates="file_links")
    file = relationship("FileRecord", back_populates="task_links")


Index("ix_task_files_task_role", TaskFileRecord.task_id, TaskFileRecord.role)
Index("ix_tasks_user_status_deleted", TaskRecord.user_id, TaskRecord.status, TaskRecord.is_deleted)
