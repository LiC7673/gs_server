from typing import Optional

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.schemas.upload import (
    UploadInitRequest, UploadInitResponse,
    UploadPartResponse,
    UploadStatusResponse,
    UploadMergeRequest,
    UploadMergeResponse,
)
from app.services.upload_service import UploadService
from app.services.file_service import FileService
from app.services.media_service import MediaService
from app.models.file import FileCategory
from app.models.task import TaskFileRole, TaskRecord, TaskStatus
from app.models.user import User
from app.services.task_service import TaskService
from app.core.exceptions import TaskStateException

router = APIRouter(prefix="/upload", tags=["upload"])


def _merge_response(
    file_record,
    *,
    task_id: Optional[str] = None,
    already_uploaded: bool = False,
) -> UploadMergeResponse:
    return UploadMergeResponse(
        task_id=task_id,
        file_id=file_record.storage_key,
        image_id=file_record.storage_key
        if file_record.category == FileCategory.MULTI_VIEW_IMAGE
        else None,
        file_hash=file_record.file_hash,
        storage_key=file_record.storage_key,
        verified=bool(file_record.file_hash),
        already_uploaded=already_uploaded,
        media_processing_status=file_record.media_processing_status,
        thumbnail_id=MediaService.thumbnail_id(file_record),
    )


async def _prepare_upload_task(
    db: AsyncSession,
    current_user: User,
    task_id: Optional[str],
) -> Optional[TaskRecord]:
    if not task_id:
        return None
    task = await TaskService.get_by_public_id(db, task_id)
    TaskService.ensure_owner(task, current_user)
    if task.status != TaskStatus.PENDING or task.current_stage not in {"task_created", "data_uploading"}:
        raise TaskStateException("Files can only be uploaded before Gaussian reconstruction starts")
    task.current_stage = "data_uploading"
    task.progress = max(float(task.progress or 0.0), 5.0)
    await db.flush()
    return task


async def _bound_upload_task(db: AsyncSession, task_record_id: Optional[int]) -> Optional[TaskRecord]:
    if not task_record_id:
        return None
    return await db.get(TaskRecord, task_record_id)


async def _attach_task_input(
    db: AsyncSession,
    task: Optional[TaskRecord],
    file_record,
) -> None:
    if not task:
        return
    await TaskService.add_file_link(db, task, file_record, TaskFileRole.INPUT)
    task.current_stage = "data_uploading"
    task.progress = max(float(task.progress or 0.0), 5.0)
    await db.flush()


@router.post("/init", response_model=UploadInitResponse)
async def init_upload(
    body: UploadInitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _prepare_upload_task(db, current_user, body.task_id)
    existing_file = await UploadService.find_existing_file(
        db,
        current_user.id,
        body.file_hash,
        body.file_size,
    )
    if existing_file:
        actual_chunk_size = body.chunk_size or settings.upload_chunk_size
        completed_upload = await UploadService.create_completed_duplicate_upload(
            db=db,
            user_id=current_user.id,
            filename=body.filename,
            mime_type=body.mime_type,
            chunk_size=actual_chunk_size,
            existing_file=existing_file,
            task_record_id=task.id if task else None,
        )
        await _attach_task_input(db, task, existing_file)
        await MediaService.ensure_enqueued(db, existing_file)
        return UploadInitResponse(
            task_id=task.public_id if task else None,
            upload_id=completed_upload.upload_id,
            chunk_size=actual_chunk_size,
            total_chunks=0,
            expires_at=completed_upload.expired_at,
            already_uploaded=True,
            file_id=existing_file.storage_key,
            image_id=existing_file.storage_key
            if existing_file.category == FileCategory.MULTI_VIEW_IMAGE
            else None,
            file_hash=existing_file.file_hash,
            storage_key=existing_file.storage_key,
            media_processing_status=existing_file.media_processing_status,
            thumbnail_id=MediaService.thumbnail_id(existing_file),
        )

    record = await UploadService.init_upload(
        db,
        current_user.id,
        body.filename,
        body.file_size,
        body.mime_type,
        body.file_hash,
        body.chunk_size,
        task_record_id=task.id if task else None,
    )
    return UploadInitResponse(
        task_id=task.public_id if task else None,
        upload_id=record.upload_id,
        chunk_size=record.chunk_size,
        total_chunks=record.total_chunks,
        expires_at=record.expired_at,
        already_uploaded=False,
    )


@router.put("/{upload_id}/chunk", response_model=UploadPartResponse)
async def upload_chunk(
    upload_id: str,
    chunk_index: int = Query(..., ge=0),
    chunk: bytes = Body(..., media_type="application/octet-stream"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _, etag = await UploadService.receive_chunk(
        db, upload_id, current_user.id, chunk_index, chunk
    )
    return UploadPartResponse(received=True, chunk_index=chunk_index, etag=etag)


@router.get("/{upload_id}/progress", response_model=UploadStatusResponse)
async def upload_progress(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = await UploadService.get_progress(db, upload_id, current_user.id)
    response = UploadStatusResponse.model_validate(record)
    task = await _bound_upload_task(db, record.task_record_id)
    response.task_id = task.public_id if task else None
    response.chunk_statuses = await UploadService.get_chunk_statuses(db, upload_id, current_user.id)
    return response


@router.post("/{upload_id}/merge", response_model=UploadMergeResponse)
async def merge_upload(
    upload_id: str,
    body: UploadMergeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    completed_file = await UploadService.find_existing_file_for_completed_upload(
        db,
        upload_id,
        current_user.id,
    )
    if completed_file:
        record = await UploadService.get_progress(db, upload_id, current_user.id)
        task = await _bound_upload_task(db, record.task_record_id)
        await _attach_task_input(db, task, completed_file)
        await MediaService.ensure_enqueued(db, completed_file)
        return _merge_response(
            completed_file,
            task_id=task.public_id if task else None,
            already_uploaded=True,
        )

    storage_object, file_hash = await UploadService.merge_chunks(
        db,
        upload_id,
        current_user.id,
        body.expected_hash,
        body.expected_size,
        [(part.chunk_index, part.etag) for part in body.parts],
    )
    record = await UploadService.get_progress(db, upload_id, current_user.id)
    task = await _bound_upload_task(db, record.task_record_id)
    category = UploadService.file_category_for_mime_type(record.mime_type)
    existing_file = await UploadService.find_existing_file(
        db,
        current_user.id,
        file_hash,
        record.file_size,
    )
    if existing_file:
        await UploadService.attach_file(record, existing_file, db)
        await _attach_task_input(db, task, existing_file)
        await MediaService.ensure_enqueued(db, existing_file)
        return _merge_response(
            existing_file,
            task_id=task.public_id if task else None,
            already_uploaded=True,
        )

    file_record = await FileService.create_record(
        db=db,
        user_id=current_user.id,
        filename=record.filename,
        original_name=record.filename,
        category=category,
        storage_object=storage_object,
        file_size=record.file_size,
        mime_type=record.mime_type,
        file_hash=file_hash,
    )
    await UploadService.attach_file(record, file_record, db)
    await _attach_task_input(db, task, file_record)
    await MediaService.enqueue(db, file_record)
    return _merge_response(file_record, task_id=task.public_id if task else None)


@router.post("/{upload_id}/cancel", response_model=dict)
async def cancel_upload(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await UploadService.cancel_upload(db, upload_id, current_user.id)
    return {"cancelled": True, "upload_id": upload_id}
