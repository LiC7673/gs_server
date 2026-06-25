import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import status
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.core.config import settings
from app.core.exceptions import AppException, NotFoundException
from app.core.redis_client import read_json, write_json
from app.core.storage import get_storage_backend
from app.models.file import (
    FileCategory,
    FileDerivativeRecord,
    FileDerivativeVariant,
    FileRecord,
    FileType,
    MediaProcessingStatus,
    StorageObject,
)
from app.models.task import TaskFileRecord, TaskFileRole, TaskRecord, TaskStatus, TaskVisibility
from app.models.user import User
from app.services.user_service import UserService


class FileService:
    MEDIA_SOURCE_CATEGORIES = {FileCategory.MULTI_VIEW_IMAGE, FileCategory.ORIGINAL_VIDEO}

    @staticmethod
    def load_options():
        return (
            selectinload(FileRecord.storage_object),
            selectinload(FileRecord.derivatives)
            .selectinload(FileDerivativeRecord.derivative_file)
            .selectinload(FileRecord.storage_object),
            selectinload(FileRecord.source_link).selectinload(FileDerivativeRecord.source_file),
        )

    @staticmethod
    def file_type_for(category: FileCategory, mime_type: str) -> FileType:
        normalized = (mime_type or "").lower()
        if normalized.startswith("image/"):
            return FileType.IMAGE
        if normalized.startswith("video/"):
            return FileType.VIDEO
        if normalized.startswith("model/") or category in {
            FileCategory.PLY_MODEL,
            FileCategory.SPLAT_MODEL,
            FileCategory.GLB_MODEL,
            FileCategory.MESH_MODEL,
        }:
            return FileType.MODEL
        return FileType.OTHER

    @staticmethod
    def is_media_source(record: FileRecord) -> bool:
        return record.category in FileService.MEDIA_SOURCE_CATEGORIES

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def object_key(user_id: int, file_hash: str, file_size: int) -> str:
        return f"objects/{user_id}/{file_hash[:2]}/{file_hash}_{file_size}"

    @staticmethod
    def _download_session_key(download_id: str) -> str:
        return f"download:{download_id}"

    @staticmethod
    def _merge_ranges(ranges: List[List[int]]) -> List[List[int]]:
        valid = sorted(
            [[int(start), int(end)] for start, end in ranges if int(start) <= int(end)],
            key=lambda item: item[0],
        )
        merged: List[List[int]] = []
        for start, end in valid:
            if not merged or start > merged[-1][1] + 1:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return merged

    @staticmethod
    def _chunk_bounds(chunk_index: int, chunk_size: int, file_size: int) -> Tuple[int, int]:
        start = chunk_index * chunk_size
        return start, min(start + chunk_size - 1, file_size - 1)

    @staticmethod
    def _apply_download_progress(session: Dict[str, Any]) -> Dict[str, Any]:
        ranges = FileService._merge_ranges(session.get("downloaded_ranges", []))
        file_size = int(session.get("file_size") or 0)
        chunk_size = int(session.get("chunk_size") or settings.download_chunk_size)
        total_chunks = (file_size + chunk_size - 1) // chunk_size if file_size else 0
        statuses: List[int] = []
        for index in range(total_chunks):
            start, end = FileService._chunk_bounds(index, chunk_size, file_size)
            statuses.append(2 if any(a <= start and b >= end for a, b in ranges) else 0)
        downloaded_bytes = min(sum(end - start + 1 for start, end in ranges), file_size)
        session["downloaded_ranges"] = ranges
        session["chunk_size"] = chunk_size
        session["total_chunks"] = total_chunks
        session["chunk_statuses"] = statuses
        session["downloaded_chunks"] = sum(item == 2 for item in statuses)
        session["downloaded_bytes"] = downloaded_bytes
        session["progress"] = round(downloaded_bytes / file_size * 100, 2) if file_size else 0.0
        if session.get("status") == "completed":
            session["completed_at"] = session.get("completed_at") or FileService._utc_now_iso()
        elif total_chunks and session["downloaded_chunks"] == total_chunks:
            session["status"] = "downloaded"
        elif downloaded_bytes:
            session["status"] = "downloading"
        else:
            session["status"] = "initialized"
        return session

    @staticmethod
    async def _write_download_session(session: Dict[str, Any]) -> None:
        session["updated_at"] = FileService._utc_now_iso()
        FileService._apply_download_progress(session)
        await write_json(
            FileService._download_session_key(session["download_id"]),
            session,
            settings.download_session_ttl_seconds,
        )

    @staticmethod
    async def _read_download_session(download_id: str) -> Dict[str, Any]:
        session = await read_json(FileService._download_session_key(download_id))
        return FileService._apply_download_progress(session)

    @staticmethod
    async def find_storage_object(
        db: AsyncSession,
        user_id: int,
        file_hash: str,
        file_size: int,
    ) -> Optional[StorageObject]:
        result = await db.execute(
            select(StorageObject).where(
                StorageObject.owner_user_id == user_id,
                StorageObject.file_hash == file_hash.lower(),
                StorageObject.file_size == file_size,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_or_create_storage_object(
        db: AsyncSession,
        user_id: int,
        file_hash: str,
        file_size: int,
    ) -> Tuple[StorageObject, bool]:
        existing = await FileService.find_storage_object(db, user_id, file_hash, file_size)
        if existing:
            existing.pending_delete = False
            return existing, False
        storage_object = StorageObject(
            owner_user_id=user_id,
            file_hash=file_hash.lower(),
            file_size=file_size,
            object_key=FileService.object_key(user_id, file_hash.lower(), file_size),
        )
        db.add(storage_object)
        await db.flush()
        user = await db.get(User, user_id)
        if user:
            user.storage_used = int(user.storage_used or 0) + file_size
        return storage_object, True

    @staticmethod
    async def create_record(
        db: AsyncSession,
        user_id: int,
        filename: str,
        original_name: str,
        category: FileCategory,
        storage_object: StorageObject,
        file_size: int,
        mime_type: str,
        file_hash: str,
        file_type: Optional[FileType] = None,
        metainfo: Optional[Dict[str, Any]] = None,
        media_processing_status: Optional[MediaProcessingStatus] = None,
    ) -> FileRecord:
        actual_file_type = file_type or FileService.file_type_for(category, mime_type)
        actual_processing_status = media_processing_status or (
            MediaProcessingStatus.PENDING
            if category in FileService.MEDIA_SOURCE_CATEGORIES
            else MediaProcessingStatus.SKIPPED
        )
        record = FileRecord(
            user_id=user_id,
            storage_object=storage_object,
            filename=filename,
            original_name=original_name,
            category=category,
            file_type=actual_file_type,
            mime_type=mime_type,
            file_size=file_size,
            file_hash=file_hash.lower(),
            metainfo={"size_bytes": file_size, **(metainfo or {})},
            media_processing_status=actual_processing_status,
        )
        db.add(record)
        await db.flush()
        await db.refresh(record)
        return record

    @staticmethod
    async def create_record_from_path(
        db: AsyncSession,
        user_id: int,
        path: Path,
        filename: str,
        category: FileCategory,
        mime_type: str,
        enforce_quota: bool = False,
        metainfo: Optional[Dict[str, Any]] = None,
    ) -> FileRecord:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        file_hash = digest.hexdigest()
        file_size = path.stat().st_size
        storage_object = await FileService.find_storage_object(db, user_id, file_hash, file_size)
        if not storage_object and enforce_quota and not await UserService.check_storage_quota(db, user_id, file_size):
            from app.core.exceptions import QuotaExceededException

            raise QuotaExceededException("Storage quota exceeded")
        storage_object, created = await FileService.get_or_create_storage_object(db, user_id, file_hash, file_size)
        storage = get_storage_backend()
        if created or not await storage.exists(storage_object.object_key):
            await storage.upload_file(storage_object.object_key, path)
        return await FileService.create_record(
            db=db,
            user_id=user_id,
            filename=filename,
            original_name=path.name,
            category=category,
            storage_object=storage_object,
            file_size=file_size,
            mime_type=mime_type,
            file_hash=file_hash,
            metainfo=metainfo,
        )

    @staticmethod
    async def replace_record_storage(
        db: AsyncSession,
        record: FileRecord,
        storage_object: StorageObject,
        *,
        filename: Optional[str],
        file_size: int,
        mime_type: str,
        file_hash: str,
        metainfo: Optional[Dict[str, Any]] = None,
    ) -> FileRecord:
        old_storage_object_id = record.storage_object_id
        record.storage_object = storage_object
        record.storage_object_id = storage_object.id
        if filename:
            record.original_name = filename
        record.file_size = file_size
        record.file_hash = file_hash.lower()
        record.mime_type = mime_type
        record.file_type = FileService.file_type_for(record.category, mime_type)
        record.metainfo = {
            **(record.metainfo or {}),
            "size_bytes": file_size,
            **(metainfo or {}),
        }
        await db.flush()
        if old_storage_object_id != storage_object.id:
            old_object = await db.get(StorageObject, old_storage_object_id)
            if old_object:
                references = await db.scalar(
                    select(func.count())
                    .select_from(FileRecord)
                    .where(
                        FileRecord.storage_object_id == old_object.id,
                        FileRecord.is_deleted.is_(False),
                    )
                )
                if not references:
                    old_object.pending_delete = True
        await db.refresh(record)
        return record

    @staticmethod
    async def find_user_file_by_hash(
        db: AsyncSession, user_id: int, file_hash: str, file_size: int
    ) -> Optional[FileRecord]:
        result = await db.execute(
            select(FileRecord)
            .options(*FileService.load_options())
            .where(
                FileRecord.user_id == user_id,
                FileRecord.file_hash == file_hash.lower(),
                FileRecord.file_size == file_size,
                FileRecord.is_deleted.is_(False),
            )
            .order_by(FileRecord.created_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _get_by_public_id(db: AsyncSession, file_id: str) -> FileRecord:
        result = await db.execute(
            select(FileRecord)
            .options(*FileService.load_options(), selectinload(FileRecord.task_links))
            .where(FileRecord.public_id == file_id, FileRecord.is_deleted.is_(False))
            .execution_options(populate_existing=True)
        )
        record = result.scalar_one_or_none()
        if not record:
            raise NotFoundException("File not found")
        return record

    @staticmethod
    async def get_accessible_file(
        db: AsyncSession,
        file_id: str,
        current_user: User,
        *,
        write: bool = False,
    ) -> FileRecord:
        record = await FileService._get_by_public_id(db, file_id.strip())
        if current_user.is_admin or record.user_id == current_user.id:
            return record
        if write:
            raise NotFoundException("File not found")
        ply_link = aliased(TaskFileRecord)
        ply_file = aliased(FileRecord)
        public_task_has_ply = exists(
            select(ply_link.id)
            .join(ply_file, ply_file.id == ply_link.file_id)
            .where(
                ply_link.task_id == TaskRecord.id,
                ply_link.role == TaskFileRole.RESULT,
                ply_file.category == FileCategory.PLY_MODEL,
                ply_file.is_deleted.is_(False),
            )
        )
        public_link = await db.execute(
            select(TaskFileRecord.id)
            .join(TaskRecord, TaskRecord.id == TaskFileRecord.task_id)
            .where(
                TaskFileRecord.file_id == record.id,
                TaskFileRecord.role.in_([TaskFileRole.RESULT, TaskFileRole.PREVIEW]),
                TaskRecord.visibility == TaskVisibility.PUBLIC,
                TaskRecord.is_deleted.is_(False),
                public_task_has_ply,
            )
            .limit(1)
        )
        if public_link.scalar_one_or_none() is not None:
            return record
        if record.source_link:
            await FileService.get_accessible_file(
                db,
                record.source_link.source_file.public_id,
                current_user,
            )
            return record
        raise NotFoundException("File not found")

    @staticmethod
    async def get_by_identifier_for_user(db: AsyncSession, file_id: str, user_id: int) -> FileRecord:
        record = await FileService._get_by_public_id(db, file_id.strip())
        if record.user_id != user_id:
            raise NotFoundException("File not found")
        return record

    get_by_storage_key_for_user = get_by_identifier_for_user

    @staticmethod
    async def list_by_user(
        db: AsyncSession,
        user_id: int,
        category: Optional[FileCategory] = None,
        file_type: Optional[FileType] = None,
        include_derivatives: bool = False,
        file_hash: Optional[str] = None,
        file_size: Optional[int] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> Tuple[List[FileRecord], int]:
        clauses = [FileRecord.user_id == user_id, FileRecord.is_deleted.is_(False)]
        if category:
            clauses.append(FileRecord.category == category)
        if file_type:
            clauses.append(FileRecord.file_type == file_type)
        if not include_derivatives:
            clauses.append(~FileRecord.source_link.has())
        if file_hash:
            clauses.append(FileRecord.file_hash == file_hash.lower())
        if file_size is not None:
            clauses.append(FileRecord.file_size == file_size)
        query = (
            select(FileRecord)
            .options(*FileService.load_options())
            .where(*clauses)
            .order_by(FileRecord.created_at.desc())
            .offset(skip)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        result = await db.execute(query)
        total = await db.scalar(select(func.count()).select_from(FileRecord).where(*clauses))
        return list(result.scalars().all()), int(total or 0)

    @staticmethod
    async def get_derivative(
        db: AsyncSession,
        source_file_id: int,
        variant: FileDerivativeVariant,
    ) -> Optional[FileDerivativeRecord]:
        result = await db.execute(
            select(FileDerivativeRecord)
            .options(
                selectinload(FileDerivativeRecord.derivative_file).selectinload(FileRecord.storage_object)
            )
            .where(
                FileDerivativeRecord.source_file_id == source_file_id,
                FileDerivativeRecord.variant == variant,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def link_derivative(
        db: AsyncSession,
        source: FileRecord,
        derivative: FileRecord,
        variant: FileDerivativeVariant,
    ) -> FileDerivativeRecord:
        existing = await FileService.get_derivative(db, source.id, variant)
        if existing:
            if existing.derivative_file_id != derivative.id:
                await FileService._soft_delete_record(db, existing.derivative_file)
                existing.derivative_file = derivative
            return existing
        link = FileDerivativeRecord(source_file=source, derivative_file=derivative, variant=variant)
        db.add(link)
        await db.flush()
        return link

    @staticmethod
    async def _mark_storage_object_for_cleanup(db: AsyncSession, record: FileRecord) -> None:
        remaining = await db.scalar(
            select(func.count())
            .select_from(FileRecord)
            .where(
                FileRecord.storage_object_id == record.storage_object_id,
                FileRecord.is_deleted.is_(False),
            )
        )
        if not remaining:
            record.storage_object.pending_delete = True

    @staticmethod
    async def _soft_delete_record(db: AsyncSession, record: FileRecord) -> None:
        if record.is_deleted:
            return
        record.is_deleted = True
        await db.flush()
        await FileService._mark_storage_object_for_cleanup(db, record)

    @staticmethod
    async def delete_file(db: AsyncSession, file_id: str, current_user: User) -> FileRecord:
        record = await FileService.get_accessible_file(db, file_id, current_user, write=True)
        links = list(
            (
                await db.execute(
                    select(TaskFileRecord)
                    .options(selectinload(TaskFileRecord.task))
                    .where(TaskFileRecord.file_id == record.id)
                )
            ).scalars()
        )
        from app.services.task_service import TaskService

        for link in links:
            task = link.task
            if link.role == TaskFileRole.INPUT and task.status in {
                TaskStatus.PENDING,
                TaskStatus.QUEUED,
                TaskStatus.PROCESSING,
            }:
                await TaskService.request_cancel(db, task, "Input file deleted by owner")
            if link.role in {TaskFileRole.RESULT, TaskFileRole.PREVIEW}:
                task.visibility = TaskVisibility.PRIVATE
            await db.delete(link)

        for derivative_link in list(record.derivatives):
            await FileService._soft_delete_record(db, derivative_link.derivative_file)
        await FileService._soft_delete_record(db, record)
        return record

    @staticmethod
    async def archive_file(db: AsyncSession, file_id: str, current_user: User) -> FileRecord:
        record = await FileService.get_accessible_file(db, file_id, current_user, write=True)
        record.is_archived = True
        await db.flush()
        return record

    @staticmethod
    async def create_download_session(
        db: AsyncSession,
        file_id: str,
        current_user: User,
        chunk_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        record = await FileService.get_accessible_file(db, file_id, current_user)
        actual_chunk_size = chunk_size or settings.download_chunk_size
        if actual_chunk_size <= 0:
            raise AppException("chunk_size must be greater than 0")
        total_chunks = (record.file_size + actual_chunk_size - 1) // actual_chunk_size if record.file_size else 0
        now = FileService._utc_now_iso()
        session = {
            "download_id": str(uuid4()),
            "user_id": current_user.id,
            "file_id": record.public_id,
            "filename": record.original_name or record.filename,
            "mime_type": record.mime_type,
            "file_size": record.file_size,
            "file_hash": record.file_hash,
            "chunk_size": actual_chunk_size,
            "total_chunks": total_chunks,
            "downloaded_chunks": 0,
            "downloaded_bytes": 0,
            "downloaded_ranges": [],
            "chunk_statuses": [0] * total_chunks,
            "chunk_etags": {},
            "progress": 0.0,
            "status": "initialized",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        await FileService._write_download_session(session)
        return session

    @staticmethod
    async def get_download_session(download_id: str, user_id: int) -> Dict[str, Any]:
        session = await FileService._read_download_session(download_id)
        if int(session.get("user_id") or 0) != user_id:
            raise NotFoundException("Download session not found")
        return session

    @staticmethod
    async def record_download_chunk(
        download_id: str,
        user_id: int,
        file_id: str,
        chunk_index: int,
        start: int,
        end: int,
        etag: str,
    ) -> Dict[str, Any]:
        session = await FileService.get_download_session(download_id, user_id)
        if session.get("file_id") != file_id:
            raise NotFoundException("Download session not found")
        session.setdefault("downloaded_ranges", []).append([start, end])
        session.setdefault("chunk_etags", {})[str(chunk_index)] = etag.lower()
        await FileService._write_download_session(session)
        return await FileService.get_download_session(download_id, user_id)

    @staticmethod
    async def complete_download_session(
        download_id: str,
        user_id: int,
        expected_hash: str,
        expected_size: int,
        parts: List[Tuple[int, str]],
    ) -> Dict[str, Any]:
        session = await FileService.get_download_session(download_id, user_id)
        if expected_size and expected_size != int(session["file_size"]):
            raise AppException("Expected size mismatch", status.HTTP_409_CONFLICT)
        if expected_hash and expected_hash.lower() != str(session["file_hash"]).lower():
            raise AppException("Expected hash mismatch", status.HTTP_409_CONFLICT)
        total_chunks = int(session["total_chunks"])
        if len(parts) != total_chunks or int(session["downloaded_chunks"]) != total_chunks:
            raise AppException("Not all chunks downloaded", status.HTTP_409_CONFLICT)
        expected = session.get("chunk_etags", {})
        seen = set()
        for index, etag in parts:
            if index in seen or index < 0 or index >= total_chunks:
                raise AppException("Invalid chunk index in parts")
            seen.add(index)
            if expected.get(str(index)) != etag.lower():
                raise AppException(f"Chunk etag mismatch for index {index}", status.HTTP_409_CONFLICT)
        session["status"] = "completed"
        await FileService._write_download_session(session)
        return await FileService.get_download_session(download_id, user_id)
