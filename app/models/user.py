import datetime
from typing import Optional

from sqlalchemy import Integer, String, Boolean, BigInteger, Date, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    nickname: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    storage_used: Mapped[int] = mapped_column(BigInteger, default=0)
    storage_quota: Mapped[int] = mapped_column(BigInteger, default=50 * 1024 * 1024 * 1024)
    task_count: Mapped[int] = mapped_column(Integer, default=0)
    task_quota: Mapped[int] = mapped_column(Integer, default=10)
    gpu_seconds_used: Mapped[int] = mapped_column(Integer, default=0)
    gpu_quota: Mapped[int] = mapped_column(Integer, default=3600)
    gpu_concurrency_quota: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    gpu_usage_date: Mapped[Optional[datetime.date]] = mapped_column(Date, nullable=True)
    avatar_file_record_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    files = relationship("FileRecord", back_populates="owner", foreign_keys="FileRecord.user_id")
    tasks = relationship("TaskRecord", back_populates="owner")
    storage_objects = relationship("StorageObject", back_populates="owner")
    avatar_file = relationship("FileRecord", foreign_keys=[avatar_file_record_id])
