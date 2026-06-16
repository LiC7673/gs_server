import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.algorithm_stages import GAUSSIAN_ALGORITHMS, MESH_ALGORITHMS
from app.core.config import settings
from app.core.exceptions import NotFoundException, TaskStateException
from app.models.file import FileCategory, FileRecord
from app.models.task import TaskFileRecord, TaskFileRole, TaskRecord, TaskStatus, TaskVisibility
from app.models.user import User
from app.services.user_service import UserService


class TaskService:
    ACTIVE_STATUSES = {TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.PROCESSING}

    @staticmethod
    async def create_task(
        db: AsyncSession,
        user_id: int,
        title: str,
        algorithm: str,
        params: dict,
    ) -> TaskRecord:
        await UserService.ensure_active_task_quota(db, user_id)
        task = TaskRecord(
            user_id=user_id,
            title=title,
            algorithm=algorithm,
            params=json.dumps(params or {}, ensure_ascii=False),
            gaussian_algorithm=algorithm,
            gaussian_params=json.dumps(params or {}, ensure_ascii=False),
            mesh_algorithm=None,
            mesh_params="{}",
            status=TaskStatus.PENDING,
            visibility=TaskVisibility.PRIVATE,
            current_stage="task_created",
        )
        db.add(task)
        await db.flush()
        user = await db.get(User, user_id)
        if user:
            user.task_count = await UserService.count_active_tasks(db, user_id)
        await db.refresh(task)
        return task

    @staticmethod
    async def get_by_public_id(db: AsyncSession, task_id: str, *, include_deleted: bool = False) -> TaskRecord:
        clauses = [TaskRecord.public_id == task_id]
        if not include_deleted:
            clauses.append(TaskRecord.is_deleted.is_(False))
        result = await db.execute(
            select(TaskRecord)
            .options(
                selectinload(TaskRecord.owner),
                selectinload(TaskRecord.file_links).selectinload(TaskFileRecord.file),
            )
            .where(*clauses)
            .execution_options(populate_existing=True)
        )
        task = result.scalar_one_or_none()
        if not task:
            raise NotFoundException("Reconstruction task not found")
        return task

    @staticmethod
    def ensure_owner(task: TaskRecord, current_user: User) -> None:
        if not current_user.is_admin and task.user_id != current_user.id:
            raise NotFoundException("Reconstruction task not found")

    @staticmethod
    def ensure_readable(task: TaskRecord, current_user: User) -> None:
        if current_user.is_admin or task.user_id == current_user.id:
            return
        if task.visibility == TaskVisibility.PUBLIC and TaskService.has_ply_result(task):
            return
        raise NotFoundException("Reconstruction task not found")

    @staticmethod
    def has_ply_result(task: TaskRecord) -> bool:
        return any(
            link.role == TaskFileRole.RESULT
            and link.file
            and not link.file.is_deleted
            and link.file.category == FileCategory.PLY_MODEL
            for link in task.file_links
        )

    @staticmethod
    async def list_owned(
        db: AsyncSession,
        current_user: User,
        status: Optional[TaskStatus],
        skip: int,
        limit: int,
    ) -> Tuple[List[TaskRecord], int]:
        clauses = [TaskRecord.user_id == current_user.id, TaskRecord.is_deleted.is_(False)]
        if status:
            clauses.append(TaskRecord.status == status)
        query = (
            select(TaskRecord)
            .options(
                selectinload(TaskRecord.file_links).selectinload(TaskFileRecord.file),
            )
            .where(*clauses)
            .order_by(TaskRecord.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        tasks = list((await db.execute(query)).scalars().all())
        total = await db.scalar(select(func.count()).select_from(TaskRecord).where(*clauses))
        return tasks, int(total or 0)

    @staticmethod
    async def list_discover(db: AsyncSession, skip: int, limit: int) -> Tuple[List[TaskRecord], int]:
        has_ply_result = exists(
            select(TaskFileRecord.id)
            .join(FileRecord, FileRecord.id == TaskFileRecord.file_id)
            .where(
                TaskFileRecord.task_id == TaskRecord.id,
                TaskFileRecord.role == TaskFileRole.RESULT,
                FileRecord.category == FileCategory.PLY_MODEL,
                FileRecord.is_deleted.is_(False),
            )
        )
        clauses = [
            TaskRecord.visibility == TaskVisibility.PUBLIC,
            TaskRecord.is_deleted.is_(False),
            has_ply_result,
        ]
        sort_time = func.coalesce(TaskRecord.completed_at, TaskRecord.updated_at, TaskRecord.created_at)
        query = (
            select(TaskRecord)
            .options(
                selectinload(TaskRecord.file_links).selectinload(TaskFileRecord.file),
            )
            .where(*clauses)
            .order_by(sort_time.desc(), TaskRecord.id.desc())
            .offset(skip)
            .limit(limit)
        )
        tasks = list((await db.execute(query)).scalars().all())
        count_query = select(func.count()).select_from(TaskRecord).where(*clauses)
        return tasks, int(await db.scalar(count_query) or 0)

    @staticmethod
    async def add_file_link(
        db: AsyncSession,
        task: TaskRecord,
        file: FileRecord,
        role: TaskFileRole,
    ) -> TaskFileRecord:
        result = await db.execute(
            select(TaskFileRecord).where(
                TaskFileRecord.task_id == task.id,
                TaskFileRecord.file_id == file.id,
                TaskFileRecord.role == role,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing
        link = TaskFileRecord(task=task, file=file, role=role)
        db.add(link)
        await db.flush()
        return link

    @staticmethod
    async def request_cancel(db: AsyncSession, task: TaskRecord, reason: str = "Task cancelled") -> TaskRecord:
        if task.status not in TaskService.ACTIVE_STATUSES:
            return task
        task.cancel_requested = True
        task.error_code = "CANCEL_REQUESTED"
        task.error_status_code = 409
        task.error_message = reason[:1000]
        if task.celery_task_id:
            from app.core.celery_app import celery_app

            try:
                celery_app.control.revoke(
                    task.celery_task_id,
                    terminate=task.status == TaskStatus.PROCESSING,
                    signal="SIGTERM",
                )
            except Exception:
                pass
        if task.gpu_billing_started_at:
            await UserService.settle_gpu_usage(db, task)
        task.status = TaskStatus.CANCELLED
        task.current_stage = "cancelled"
        task.progress = 100.0
        task.completed_at = datetime.now(timezone.utc)
        await db.flush()
        return task

    @staticmethod
    async def set_visibility(
        db: AsyncSession, task: TaskRecord, current_user: User, visibility: TaskVisibility
    ) -> TaskRecord:
        TaskService.ensure_owner(task, current_user)
        if visibility == TaskVisibility.PUBLIC and task.status != TaskStatus.COMPLETED:
            raise TaskStateException("Only completed tasks can be public")
        task.visibility = visibility
        await db.flush()
        return task

    @staticmethod
    async def delete_task(db: AsyncSession, task: TaskRecord, current_user: User) -> TaskRecord:
        TaskService.ensure_owner(task, current_user)
        if task.status in TaskService.ACTIVE_STATUSES:
            await TaskService.request_cancel(db, task, "Task deleted by owner")
        task.visibility = TaskVisibility.PRIVATE
        task.is_deleted = True
        for link in list(task.file_links):
            await db.delete(link)
        user = await db.get(User, task.user_id)
        if user:
            user.task_count = await UserService.count_active_tasks(db, task.user_id)
        await db.flush()
        return task

    @staticmethod
    async def recover_stale_tasks(db: AsyncSession) -> List[TaskRecord]:
        threshold = datetime.now(timezone.utc) - timedelta(seconds=settings.reconstruction_stale_processing_seconds)
        result = await db.execute(
            select(TaskRecord)
            .options(
                selectinload(TaskRecord.file_links).selectinload(TaskFileRecord.file),
            )
            .where(
                TaskRecord.is_deleted.is_(False),
                or_(
                    and_(
                        TaskRecord.status == TaskStatus.PROCESSING,
                        TaskRecord.heartbeat_at < threshold,
                    ),
                    and_(
                        TaskRecord.status == TaskStatus.QUEUED,
                        TaskRecord.celery_task_id == "",
                    ),
                    and_(
                        TaskRecord.status == TaskStatus.QUEUED,
                        TaskRecord.celery_task_id == "dispatching",
                        TaskRecord.heartbeat_at < threshold,
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
        )
        recovered = list(result.scalars().all())
        for task in recovered:
            task.celery_task_id = ""
            if task.status == TaskStatus.PROCESSING:
                await UserService.settle_gpu_usage(db, task, ended_at=task.heartbeat_at or datetime.now(timezone.utc))
                task.retry_count = int(task.retry_count or 0) + 1
                if task.retry_count >= settings.task_max_retries:
                    if task.algorithm in MESH_ALGORITHMS:
                        task.status = TaskStatus.PARTIAL_COMPLETED
                        task.current_stage = "mesh_failed"
                    elif task.algorithm in GAUSSIAN_ALGORITHMS and TaskService.has_ply_result(task):
                        task.status = TaskStatus.PARTIAL_COMPLETED
                        task.current_stage = "gaussian_failed"
                    elif task.algorithm in GAUSSIAN_ALGORITHMS:
                        task.status = TaskStatus.FAILED
                        task.current_stage = "gaussian_failed"
                    else:
                        task.status = TaskStatus.FAILED
                        task.current_stage = "failed"
                    task.error_code = "WORKER_STALE"
                    task.error_status_code = 504
                    task.error_message = "GPU worker heartbeat expired; manual inspection required"
                    task.progress = 100.0
                    task.completed_at = datetime.now(timezone.utc)
                    task.queue_reason = ""
                    continue
            task.status = TaskStatus.QUEUED
            task.progress = 0.0
            task.queue_reason = ""
            task.current_stage = (
                "mesh_queued"
                if task.algorithm in MESH_ALGORITHMS
                else "gaussian_queued"
            )
        await db.flush()
        return recovered
