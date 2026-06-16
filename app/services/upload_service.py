import hashlib
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.exceptions import AppException, NotFoundException, QuotaExceededException
from app.core.storage import get_storage_backend
from app.models.file import FileCategory, FileRecord, StorageObject
from app.models.upload import UploadRecord, UploadStatus
from app.services.file_service import FileService
from app.services.user_service import UserService
from app.utils.hash import compute_chunk_hash


class UploadService:
    CHUNK_PENDING = 0
    CHUNK_UPLOADED = 2

    @staticmethod
    def _chunk_path(upload_id: str, chunk_index: int) -> str:
        return f"uploads/{upload_id}/chunk_{chunk_index:06d}"

    @staticmethod
    def file_category_for_mime_type(mime_type: str) -> FileCategory:
        normalized = (mime_type or "").strip().lower()
        if normalized.startswith("video/"):
            return FileCategory.ORIGINAL_VIDEO
        if normalized.startswith("image/"):
            return FileCategory.MULTI_VIEW_IMAGE
        if normalized == "model/ply":
            return FileCategory.PLY_MODEL
        return FileCategory.OTHER

    @staticmethod
    def _is_expired(record: UploadRecord) -> bool:
        if not record.expired_at:
            return False
        expires = record.expired_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires < datetime.now(timezone.utc)

    @staticmethod
    async def _get_upload_for_user(
        db: AsyncSession,
        upload_id: str,
        user_id: int,
        *,
        fail_if_expired: bool = False,
    ) -> UploadRecord:
        result = await db.execute(select(UploadRecord).where(UploadRecord.upload_id == upload_id))
        record = result.scalar_one_or_none()
        if not record or record.user_id != user_id:
            raise NotFoundException("Upload not found")
        if record.status == UploadStatus.EXPIRED and fail_if_expired:
            raise AppException("Upload expired", status.HTTP_410_GONE)
        if record.status not in {UploadStatus.COMPLETED, UploadStatus.CANCELLED, UploadStatus.EXPIRED}:
            if UploadService._is_expired(record):
                record.status = UploadStatus.EXPIRED
                await db.flush()
                if fail_if_expired:
                    raise AppException("Upload expired", status.HTTP_410_GONE)
        return record

    @staticmethod
    async def _count_received_chunks(upload_id: str, total_chunks: int) -> int:
        storage = get_storage_backend()
        count = 0
        for index in range(total_chunks):
            if await storage.exists(UploadService._chunk_path(upload_id, index)):
                count += 1
        return count

    @staticmethod
    async def init_upload(
        db: AsyncSession,
        user_id: int,
        filename: str,
        file_size: int,
        mime_type: str,
        file_hash: str,
        chunk_size: Optional[int] = None,
        task_record_id: Optional[int] = None,
    ) -> UploadRecord:
        if file_size <= 0:
            raise AppException("file_size must be greater than 0")
        if file_size > settings.max_file_size:
            raise AppException("File is too large", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        actual_chunk_size = chunk_size or settings.upload_chunk_size
        if actual_chunk_size <= 0:
            raise AppException("chunk_size must be greater than 0")
        existing_object = await FileService.find_storage_object(db, user_id, file_hash, file_size)
        if not existing_object and not await UserService.check_storage_quota(db, user_id, file_size):
            raise QuotaExceededException("Storage quota exceeded")
        record = UploadRecord(
            upload_id=str(uuid.uuid4()),
            user_id=user_id,
            task_record_id=task_record_id,
            filename=filename,
            mime_type=mime_type.strip().lower(),
            file_hash=file_hash.strip().lower(),
            file_size=file_size,
            chunk_size=actual_chunk_size,
            total_chunks=(file_size + actual_chunk_size - 1) // actual_chunk_size,
            status=UploadStatus.INITIATED,
            expired_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        db.add(record)
        await db.flush()
        return record

    @staticmethod
    async def find_existing_file(
        db: AsyncSession,
        user_id: int,
        file_hash: str,
        file_size: int,
    ) -> Optional[FileRecord]:
        return await FileService.find_user_file_by_hash(db, user_id, file_hash, file_size)

    @staticmethod
    async def find_existing_file_for_completed_upload(
        db: AsyncSession,
        upload_id: str,
        user_id: int,
    ) -> Optional[FileRecord]:
        record = await UploadService._get_upload_for_user(db, upload_id, user_id)
        if record.status != UploadStatus.COMPLETED or not record.file_id:
            return None
        result = await db.execute(
            select(FileRecord)
            .options(*FileService.load_options())
            .where(FileRecord.id == record.file_id, FileRecord.is_deleted.is_(False))
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create_completed_duplicate_upload(
        db: AsyncSession,
        user_id: int,
        filename: str,
        mime_type: str,
        chunk_size: int,
        existing_file: FileRecord,
        task_record_id: Optional[int] = None,
    ) -> UploadRecord:
        record = UploadRecord(
            upload_id=str(uuid.uuid4()),
            user_id=user_id,
            task_record_id=task_record_id,
            file_id=existing_file.id,
            filename=filename,
            mime_type=mime_type.strip().lower(),
            file_hash=existing_file.file_hash,
            file_size=existing_file.file_size,
            chunk_size=chunk_size,
            total_chunks=0,
            received_chunks=0,
            status=UploadStatus.COMPLETED,
            uploaded_hash=existing_file.file_hash,
            expired_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        db.add(record)
        await db.flush()
        return record

    @staticmethod
    async def receive_chunk(
        db: AsyncSession,
        upload_id: str,
        user_id: int,
        chunk_index: int,
        chunk_data: bytes,
    ) -> Tuple[UploadRecord, str]:
        record = await UploadService._get_upload_for_user(db, upload_id, user_id, fail_if_expired=True)
        if record.status in {UploadStatus.COMPLETED, UploadStatus.CANCELLED}:
            raise AppException("Upload already completed or cancelled")
        if chunk_index < 0 or chunk_index >= record.total_chunks:
            raise AppException("Invalid chunk index")
        expected_size = min(record.chunk_size, record.file_size - chunk_index * record.chunk_size)
        if len(chunk_data) != expected_size:
            raise AppException(
                f"Invalid chunk size for index {chunk_index}: expected {expected_size}, got {len(chunk_data)}"
            )
        await get_storage_backend().save(UploadService._chunk_path(upload_id, chunk_index), chunk_data)
        record.received_chunks = await UploadService._count_received_chunks(upload_id, record.total_chunks)
        record.status = UploadStatus.UPLOADING
        await db.flush()
        return record, compute_chunk_hash(chunk_data)

    @staticmethod
    async def merge_chunks(
        db: AsyncSession,
        upload_id: str,
        user_id: int,
        expected_hash: str = "",
        expected_size: int = 0,
        parts: Optional[List[Tuple[int, str]]] = None,
    ) -> Tuple[StorageObject, str]:
        record = await UploadService._get_upload_for_user(db, upload_id, user_id, fail_if_expired=True)
        if record.status in {UploadStatus.COMPLETED, UploadStatus.CANCELLED}:
            raise AppException("Upload already completed or cancelled")
        record.received_chunks = await UploadService._count_received_chunks(upload_id, record.total_chunks)
        if record.received_chunks != record.total_chunks:
            raise AppException(f"Not all chunks received: {record.received_chunks}/{record.total_chunks}")

        expected_etags: Dict[int, str] = {}
        if parts is not None:
            if len(parts) != record.total_chunks:
                raise AppException(f"parts length mismatch: expected {record.total_chunks}, got {len(parts)}")
            for index, etag in parts:
                if index < 0 or index >= record.total_chunks or index in expected_etags:
                    raise AppException(f"Invalid or duplicate chunk index in parts: {index}")
                expected_etags[index] = etag.lower()

        storage = get_storage_backend()
        scratch = Path(settings.api_scratch_path)
        scratch.mkdir(parents=True, exist_ok=True)
        sha256 = hashlib.sha256()
        md5 = hashlib.md5()
        total_size = 0
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(dir=scratch, prefix="merge_", delete=False) as merged:
                temp_path = Path(merged.name)
                for index in range(record.total_chunks):
                    object_key = UploadService._chunk_path(upload_id, index)
                    if not await storage.exists(object_key):
                        raise AppException(f"Missing chunk: {index}")
                    chunk = await storage.read(object_key)
                    actual_etag = compute_chunk_hash(chunk)
                    if expected_etags and expected_etags.get(index) != actual_etag:
                        raise AppException(f"Chunk etag mismatch for index {index}", status.HTTP_409_CONFLICT)
                    sha256.update(chunk)
                    md5.update(chunk)
                    total_size += len(chunk)
                    merged.write(chunk)
            actual_hash = sha256.hexdigest()
            if total_size != record.file_size or (expected_size and total_size != expected_size):
                raise AppException("Merged file size mismatch", status.HTTP_409_CONFLICT)
            if expected_hash:
                actual_expected = md5.hexdigest() if len(expected_hash) == 32 else actual_hash
                if actual_expected != expected_hash.lower():
                    raise AppException("Expected hash mismatch", status.HTTP_409_CONFLICT)
            if actual_hash != record.file_hash.lower():
                raise AppException("File hash mismatch", status.HTTP_409_CONFLICT)

            storage_object, created = await FileService.get_or_create_storage_object(
                db, user_id, actual_hash, total_size
            )
            if created or not await storage.exists(storage_object.object_key):
                await storage.upload_file(storage_object.object_key, temp_path)
            for index in range(record.total_chunks):
                await storage.delete(UploadService._chunk_path(upload_id, index))
            record.status = UploadStatus.COMPLETED
            record.uploaded_hash = actual_hash
            await db.flush()
            return storage_object, actual_hash
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()

    @staticmethod
    async def attach_file(record: UploadRecord, file: FileRecord, db: AsyncSession) -> None:
        record.file_id = file.id
        await db.flush()

    @staticmethod
    async def cancel_upload(db: AsyncSession, upload_id: str, user_id: int) -> bool:
        record = await UploadService._get_upload_for_user(db, upload_id, user_id)
        if record.status == UploadStatus.COMPLETED:
            raise AppException("Cannot cancel completed upload", status.HTTP_409_CONFLICT)
        storage = get_storage_backend()
        for index in range(record.total_chunks):
            await storage.delete(UploadService._chunk_path(upload_id, index))
        record.status = UploadStatus.CANCELLED
        await db.flush()
        return True

    @staticmethod
    async def get_progress(db: AsyncSession, upload_id: str, user_id: int) -> UploadRecord:
        record = await UploadService._get_upload_for_user(db, upload_id, user_id)
        if record.status not in {UploadStatus.COMPLETED, UploadStatus.CANCELLED, UploadStatus.EXPIRED}:
            record.received_chunks = await UploadService._count_received_chunks(upload_id, record.total_chunks)
            await db.flush()
        return record

    @staticmethod
    async def get_chunk_statuses(db: AsyncSession, upload_id: str, user_id: int) -> List[int]:
        record = await UploadService._get_upload_for_user(db, upload_id, user_id)
        if record.status == UploadStatus.COMPLETED:
            return [UploadService.CHUNK_UPLOADED] * record.total_chunks
        if record.status in {UploadStatus.CANCELLED, UploadStatus.EXPIRED}:
            return [UploadService.CHUNK_PENDING] * record.total_chunks
        storage = get_storage_backend()
        return [
            UploadService.CHUNK_UPLOADED
            if await storage.exists(UploadService._chunk_path(record.upload_id, index))
            else UploadService.CHUNK_PENDING
            for index in range(record.total_chunks)
        ]
