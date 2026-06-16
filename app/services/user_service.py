from datetime import datetime, time, timezone
import math
from typing import Optional
from fastapi import status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.core.config import settings
from app.core.exceptions import AppException, NotFoundException
from app.core.quota_time import gpu_quota_resets_at, gpu_quota_zone, gpu_usage_date
from app.models.file import FileDerivativeRecord, FileRecord, FileType
from app.models.task import TaskRecord, TaskStatus
from app.models.user import User


class UserService:
    ACTIVE_TASK_STATUSES = {TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.PROCESSING}

    @staticmethod
    def load_options():
        return (
            selectinload(User.avatar_file)
            .selectinload(FileRecord.derivatives)
            .selectinload(FileDerivativeRecord.derivative_file),
        )

    @staticmethod
    async def get_by_id(db: AsyncSession, user_id: int) -> Optional[User]:
        result = await db.execute(
            select(User).options(*UserService.load_options()).where(User.id == user_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_username(db: AsyncSession, username: str) -> Optional[User]:
        result = await db.execute(
            select(User).options(*UserService.load_options()).where(User.username == username)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def update_user(db: AsyncSession, user_id: int, **kwargs) -> User:
        user = await UserService.get_by_id(db, user_id)
        if not user:
            raise NotFoundException("User not found")
        for key, value in kwargs.items():
            if value is not None and hasattr(user, key):
                setattr(user, key, value)
        await db.flush()
        return user

    @staticmethod
    async def get_for_update(db: AsyncSession, user_id: int) -> Optional[User]:
        result = await db.execute(select(User).where(User.id == user_id).with_for_update())
        return result.scalar_one_or_none()

    @staticmethod
    async def update_avatar(db: AsyncSession, user_id: int, avatar_file_id: Optional[str]) -> User:
        user = await UserService.get_by_id(db, user_id)
        if not user:
            raise NotFoundException("User not found")
        if avatar_file_id is None:
            user.avatar_file_record_id = None
            await db.flush()
            refreshed = await UserService.get_by_id(db, user_id)
            if not refreshed:
                raise NotFoundException("User not found")
            return refreshed

        normalized_file_id = avatar_file_id.strip()
        if not normalized_file_id:
            raise AppException("avatar_file_id cannot be empty", status.HTTP_400_BAD_REQUEST)

        from app.services.file_service import FileService
        from app.services.media_service import MediaService

        record = await FileService.get_by_identifier_for_user(db, normalized_file_id, user_id)
        if record.source_link:
            raise AppException(
                "Derived files cannot be used as avatar; use the original image file_id",
                status.HTTP_400_BAD_REQUEST,
            )
        if record.file_type != FileType.IMAGE and not (record.mime_type or "").lower().startswith("image/"):
            raise AppException("Avatar must be an image file", status.HTTP_400_BAD_REQUEST)

        user.avatar_file_record_id = record.id
        await db.flush()
        await MediaService.ensure_enqueued(db, record)
        refreshed = await UserService.get_by_id(db, user_id)
        if not refreshed:
            raise NotFoundException("User not found")
        return refreshed

    @staticmethod
    async def get_usage(db: AsyncSession, user_id: int) -> dict:
        user = await UserService.get_by_id(db, user_id)
        if not user:
            raise NotFoundException("User not found")
        await UserService.reset_daily_gpu_usage_if_needed(db, user)
        active_task_count = await UserService.count_active_tasks(db, user_id)
        total_task_count = await UserService.count_total_tasks(db, user_id)
        gpu_running_count = await UserService.count_gpu_running_tasks(db, user_id)
        user.task_count = active_task_count
        return {
            "storage_used": user.storage_used,
            "storage_quota": user.storage_quota,
            "task_count": active_task_count,
            "task_quota": user.task_quota,
            "total_task_count": total_task_count,
            "gpu_running_count": gpu_running_count,
            "gpu_concurrency_quota": user.gpu_concurrency_quota,
            "gpu_seconds_used": user.gpu_seconds_used,
            "gpu_quota": user.gpu_quota,
            "gpu_quota_exceeded": int(user.gpu_seconds_used or 0) >= int(user.gpu_quota or 0),
            "gpu_quota_resets_at": gpu_quota_resets_at(),
        }

    @staticmethod
    async def check_storage_quota(db: AsyncSession, user_id: int, additional: int) -> bool:
        user = await UserService.get_by_id(db, user_id)
        if not user:
            raise NotFoundException("User not found")
        return int(user.storage_used or 0) + int(additional or 0) <= int(user.storage_quota or 0)

    @staticmethod
    async def check_task_quota(db: AsyncSession, user_id: int) -> bool:
        user = await UserService.get_by_id(db, user_id)
        if not user:
            raise NotFoundException("User not found")
        return await UserService.count_active_tasks(db, user_id) < int(user.task_quota or 0)

    @staticmethod
    async def count_active_tasks(
        db: AsyncSession,
        user_id: int,
        *,
        exclude_task_record_id: Optional[int] = None,
    ) -> int:
        clauses = [
            TaskRecord.user_id == user_id,
            TaskRecord.is_deleted.is_(False),
            TaskRecord.status.in_(tuple(UserService.ACTIVE_TASK_STATUSES)),
        ]
        if exclude_task_record_id is not None:
            clauses.append(TaskRecord.id != exclude_task_record_id)
        return int(await db.scalar(select(func.count()).select_from(TaskRecord).where(*clauses)) or 0)

    @staticmethod
    async def count_total_tasks(db: AsyncSession, user_id: int) -> int:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(TaskRecord)
                .where(TaskRecord.user_id == user_id, TaskRecord.is_deleted.is_(False))
            )
            or 0
        )

    @staticmethod
    async def count_gpu_running_tasks(db: AsyncSession, user_id: int) -> int:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(TaskRecord)
                .where(
                    TaskRecord.user_id == user_id,
                    TaskRecord.is_deleted.is_(False),
                    TaskRecord.status == TaskStatus.PROCESSING,
                    TaskRecord.gpu_billing_started_at.is_not(None),
                )
            )
            or 0
        )

    @staticmethod
    async def ensure_active_task_quota(
        db: AsyncSession,
        user_id: int,
        *,
        exclude_task_record_id: Optional[int] = None,
    ) -> None:
        user = await UserService.get_for_update(db, user_id)
        if not user:
            raise NotFoundException("User not found")
        active_count = await UserService.count_active_tasks(
            db,
            user_id,
            exclude_task_record_id=exclude_task_record_id,
        )
        user.task_count = active_count
        task_quota = int(user.task_quota or 0)
        if active_count >= task_quota:
            from app.core.exceptions import QuotaExceededException

            raise QuotaExceededException(
                {
                    "code": "ACTIVE_TASK_QUOTA_EXCEEDED",
                    "message": "Active task quota exceeded",
                    "task_count": active_count,
                    "task_quota": task_quota,
                }
            )

    @staticmethod
    async def reset_daily_gpu_usage_if_needed(db: AsyncSession, user: User) -> bool:
        today = gpu_usage_date()
        if user.gpu_usage_date == today:
            return False
        user.gpu_seconds_used = 0
        user.gpu_usage_date = today
        await db.flush()
        return True

    @staticmethod
    async def reset_daily_gpu_usage_for_all(db: AsyncSession) -> int:
        today = gpu_usage_date()
        result = await db.execute(select(User).where(or_(User.gpu_usage_date.is_(None), User.gpu_usage_date != today)))
        users = list(result.scalars().all())
        for user in users:
            user.gpu_seconds_used = 0
            user.gpu_usage_date = today
        await db.flush()
        return len(users)

    @staticmethod
    async def reset_gpu_usage(db: AsyncSession, user_id: int) -> User:
        user = await UserService.get_for_update(db, user_id)
        if not user:
            raise NotFoundException("User not found")
        user.gpu_seconds_used = 0
        user.gpu_usage_date = gpu_usage_date()
        await db.flush()
        refreshed = await UserService.get_by_id(db, user_id)
        if not refreshed:
            raise NotFoundException("User not found")
        return refreshed

    @staticmethod
    async def gpu_quota_status(db: AsyncSession, user_id: int) -> dict:
        user = await UserService.get_for_update(db, user_id)
        if not user:
            raise NotFoundException("User not found")
        await UserService.reset_daily_gpu_usage_if_needed(db, user)
        used = int(user.gpu_seconds_used or 0)
        quota = int(user.gpu_quota or 0)
        return {
            "gpu_seconds_used": used,
            "gpu_quota": quota,
            "gpu_quota_exceeded": used >= quota,
            "gpu_quota_resets_at": gpu_quota_resets_at(),
            "gpu_concurrency_quota": int(user.gpu_concurrency_quota or 0),
        }

    @staticmethod
    async def ensure_gpu_daily_quota_available(db: AsyncSession, user_id: int) -> dict:
        quota = await UserService.gpu_quota_status(db, user_id)
        if quota["gpu_quota_exceeded"]:
            from app.core.exceptions import QuotaExceededException

            raise QuotaExceededException(
                {
                    "code": "GPU_DAILY_QUOTA_EXCEEDED",
                    "message": "Daily GPU quota exceeded",
                    **quota,
                }
            )
        return quota

    @staticmethod
    async def settle_gpu_usage(
        db: AsyncSession,
        task: TaskRecord,
        *,
        ended_at: Optional[datetime] = None,
    ) -> int:
        started_at = task.gpu_billing_started_at
        if not started_at:
            return 0
        ended_at = ended_at or datetime.now(timezone.utc)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=timezone.utc)
        elapsed = max(0.0, (ended_at - started_at).total_seconds())
        seconds = int(math.ceil(elapsed))
        zone = gpu_quota_zone()
        local_end = ended_at.astimezone(zone)
        current_day_start = datetime.combine(local_end.date(), time.min, tzinfo=zone).astimezone(timezone.utc)
        current_day_elapsed = max(0.0, (ended_at - max(started_at, current_day_start)).total_seconds())
        current_day_seconds = int(math.ceil(current_day_elapsed))
        user = await UserService.get_for_update(db, task.user_id)
        if user:
            await UserService.reset_daily_gpu_usage_if_needed(db, user)
            user.gpu_seconds_used = int(user.gpu_seconds_used or 0) + current_day_seconds
        task.gpu_seconds_cost = int(task.gpu_seconds_cost or 0) + seconds
        task.gpu_billing_started_at = None
        await db.flush()
        return seconds

    @staticmethod
    def default_storage_quota() -> int:
        return int(settings.default_user_storage_quota)

    @staticmethod
    def default_active_task_quota() -> int:
        return int(settings.default_user_active_task_quota)

    @staticmethod
    def default_gpu_daily_quota() -> int:
        return int(settings.default_user_gpu_daily_quota_seconds)

    @staticmethod
    def default_gpu_concurrency_quota() -> int:
        return int(settings.default_user_gpu_concurrency_quota)
