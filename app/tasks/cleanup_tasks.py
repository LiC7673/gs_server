from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.celery_app import celery_app
from app.core.database import async_session_factory
from app.core.storage import get_storage_backend
from app.models.file import FileRecord, StorageObject
from app.models.upload import UploadRecord, UploadStatus
from app.tasks.async_runner import run_async


async def _cleanup_expired_uploads() -> int:
    storage = get_storage_backend()
    removed = 0
    async with async_session_factory() as db:
        records = list(
            (
                await db.execute(
                    select(UploadRecord).where(
                        UploadRecord.expired_at < datetime.now(timezone.utc),
                        UploadRecord.status.in_([UploadStatus.INITIATED, UploadStatus.UPLOADING]),
                    )
                )
            ).scalars()
        )
        for record in records:
            for index in range(record.total_chunks):
                await storage.delete(f"uploads/{record.upload_id}/chunk_{index:06d}")
            record.status = UploadStatus.EXPIRED
            removed += 1
        await db.commit()
    return removed


async def _cleanup_orphan_objects() -> int:
    storage = get_storage_backend()
    removed = 0
    async with async_session_factory() as db:
        objects = list(
            (
                await db.execute(
                    select(StorageObject)
                    .options(selectinload(StorageObject.owner))
                    .where(StorageObject.pending_delete.is_(True))
                )
            ).scalars()
        )
        for storage_object in objects:
            references = await db.scalar(
                select(func.count())
                .select_from(FileRecord)
                .where(
                    FileRecord.storage_object_id == storage_object.id,
                    FileRecord.is_deleted.is_(False),
                )
            )
            if references:
                storage_object.pending_delete = False
                continue
            await storage.delete(storage_object.object_key)
            deleted_files = list(
                (
                    await db.execute(
                        select(FileRecord).where(
                            FileRecord.storage_object_id == storage_object.id,
                            FileRecord.is_deleted.is_(True),
                        )
                    )
                ).scalars()
            )
            for file in deleted_files:
                await db.delete(file)
            user = storage_object.owner
            if user:
                user.storage_used = max(0, int(user.storage_used or 0) - storage_object.file_size)
            await db.delete(storage_object)
            removed += 1
        await db.commit()
    return removed


async def _reset_daily_gpu_usage() -> int:
    async with async_session_factory() as db:
        from app.services.user_service import UserService

        reset_count = await UserService.reset_daily_gpu_usage_for_all(db)
        await db.commit()
        return reset_count


@celery_app.task(name="cleanup.expired_uploads")
def cleanup_expired_uploads() -> dict:
    return {"expired_uploads": run_async(_cleanup_expired_uploads())}


@celery_app.task(name="cleanup.stale_tasks")
def cleanup_stale_tasks() -> dict:
    from app.api.v1.reconstruction import recover_stale_reconstruction_tasks

    return {"requeued_tasks": run_async(recover_stale_reconstruction_tasks())}


@celery_app.task(name="cleanup.temp_files")
def cleanup_temp_files() -> dict:
    return {"orphan_objects": run_async(_cleanup_orphan_objects())}


@celery_app.task(name="cleanup.stale_media")
def cleanup_stale_media() -> dict:
    from app.services.media_service import MediaService

    return {"requeued_media_files": run_async(MediaService.recover_stale())}


@celery_app.task(name="cleanup.reset_daily_gpu_usage")
def reset_daily_gpu_usage() -> dict:
    return {"reset_users": run_async(_reset_daily_gpu_usage())}
