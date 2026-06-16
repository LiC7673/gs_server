import asyncio
import contextlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy import inspect
from sqlalchemy.orm.attributes import NO_VALUE

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.exceptions import QuotaExceededException
from app.core.storage import get_storage_backend
from app.models.file import (
    FileCategory,
    FileDerivativeVariant,
    FileRecord,
    MediaProcessingStatus,
)
from app.services.file_service import FileService


class MediaProcessingFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class MediaService:
    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def thumbnail_id(record: FileRecord) -> Optional[str]:
        links = inspect(record).attrs.derivatives.loaded_value
        if links is NO_VALUE:
            return None
        for link in links:
            if (
                link.variant == FileDerivativeVariant.THUMBNAIL
                and not link.derivative_file.is_deleted
            ):
                return link.derivative_file.public_id
        return None

    @staticmethod
    async def _get_source(db, file_id: str, *, lock: bool = False) -> Optional[FileRecord]:
        query = (
            select(FileRecord)
            .options(*FileService.load_options())
            .where(FileRecord.public_id == file_id, FileRecord.is_deleted.is_(False))
        )
        if lock:
            query = query.with_for_update()
        return (await db.execute(query)).scalar_one_or_none()

    @staticmethod
    async def enqueue(db, record: FileRecord, *, force: bool = False) -> bool:
        if not FileService.is_media_source(record) or record.is_deleted:
            return False
        if (
            not force
            and record.media_processing_status == MediaProcessingStatus.COMPLETED
            and MediaService.thumbnail_id(record)
        ):
            return False
        record.media_processing_status = MediaProcessingStatus.PENDING
        record.media_processing_error_code = ""
        record.media_processing_error = ""
        record.media_processing_task_id = "dispatching"
        record.media_processing_heartbeat_at = MediaService._utc_now()
        record.media_processed_at = None
        await db.commit()
        from app.core.celery_app import celery_app

        try:
            queued = celery_app.send_task(
                "media.process",
                args=[record.public_id],
                queue=settings.media_queue_name,
            )
        except Exception as exc:
            record.media_processing_status = MediaProcessingStatus.FAILED
            record.media_processing_error_code = "MEDIA_QUEUE_UNAVAILABLE"
            record.media_processing_error = str(exc)[:1000]
            record.media_processing_task_id = ""
            await db.commit()
            return False
        record.media_processing_task_id = queued.id
        await db.commit()
        return True

    @staticmethod
    async def ensure_enqueued(db, record: FileRecord) -> bool:
        if (
            FileService.is_media_source(record)
            and record.media_processing_status == MediaProcessingStatus.PENDING
            and not record.media_processing_task_id
        ):
            return await MediaService.enqueue(db, record)
        return False

    @staticmethod
    async def _run_command(*command: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise MediaProcessingFailure(
                "MEDIA_TOOL_FAILED",
                stderr.decode("utf-8", errors="replace")[-1000:] or f"{command[0]} exited {process.returncode}",
            )
        return stdout.decode("utf-8", errors="replace")

    @staticmethod
    def _save_thumbnail(image_path: Path, thumbnail_path: Path) -> Dict[str, Any]:
        from PIL import Image, ImageOps

        Image.MAX_IMAGE_PIXELS = settings.media_max_image_pixels
        with Image.open(image_path) as opened:
            image = ImageOps.exif_transpose(opened)
            width, height = image.size
            thumbnail = image.convert("RGB")
            thumbnail.thumbnail(
                (settings.media_thumbnail_max_edge, settings.media_thumbnail_max_edge)
            )
            thumb_width, thumb_height = thumbnail.size
            thumbnail.save(
                thumbnail_path,
                format="JPEG",
                quality=settings.media_thumbnail_jpeg_quality,
                optimize=True,
            )
        return {
            "width": width,
            "height": height,
            "thumbnail_width": thumb_width,
            "thumbnail_height": thumb_height,
        }

    @staticmethod
    def _fps(value: str) -> float:
        try:
            return round(float(Fraction(value)), 3)
        except (ValueError, ZeroDivisionError):
            return 0.0

    @staticmethod
    def _float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _int(value: Any, fallback: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    async def _video_metadata(source_path: Path, cover_path: Path) -> Dict[str, Any]:
        payload = json.loads(
            await MediaService._run_command(
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,codec_name,bit_rate:format=duration,bit_rate",
                "-of",
                "json",
                str(source_path),
            )
        )
        streams = payload.get("streams") or []
        if not streams:
            raise MediaProcessingFailure("MEDIA_VIDEO_STREAM_NOT_FOUND", "Video has no readable video stream")
        stream = streams[0]
        media_format = payload.get("format") or {}
        duration = MediaService._float(media_format.get("duration"))
        fps = MediaService._fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0")
        frame_count = MediaService._int(stream.get("nb_frames"), round(duration * fps))
        for seek in ("1", "0"):
            try:
                await MediaService._run_command(
                    "ffmpeg",
                    "-y",
                    "-ss",
                    seek,
                    "-i",
                    str(source_path),
                    "-frames:v",
                    "1",
                    str(cover_path),
                )
                if cover_path.exists() and cover_path.stat().st_size:
                    break
            except MediaProcessingFailure:
                if seek == "0":
                    raise
        return {
            "width": MediaService._int(stream.get("width")),
            "height": MediaService._int(stream.get("height")),
            "duration_seconds": round(duration, 3),
            "fps": fps,
            "frame_count": frame_count,
            "codec_name": str(stream.get("codec_name") or ""),
            "bit_rate": MediaService._int(stream.get("bit_rate") or media_format.get("bit_rate")),
        }

    @staticmethod
    async def _mark_failed(file_id: str, code: str, message: str) -> None:
        async with async_session_factory() as db:
            record = await MediaService._get_source(db, file_id, lock=True)
            if not record:
                return
            record.media_processing_status = MediaProcessingStatus.FAILED
            record.media_processing_error_code = code
            record.media_processing_error = message[:1000]
            record.media_processing_heartbeat_at = MediaService._utc_now()
            await db.commit()

    @staticmethod
    async def _heartbeat(file_id: str, celery_task_id: str) -> None:
        interval = max(5, min(30, settings.media_stale_processing_seconds // 3))
        while True:
            await asyncio.sleep(interval)
            async with async_session_factory() as db:
                await db.execute(
                    update(FileRecord)
                    .where(
                        FileRecord.public_id == file_id,
                        FileRecord.media_processing_status == MediaProcessingStatus.PROCESSING,
                        FileRecord.media_processing_task_id == celery_task_id,
                    )
                    .values(media_processing_heartbeat_at=MediaService._utc_now())
                )
                await db.commit()

    @staticmethod
    async def process(file_id: str, celery_task_id: str) -> str:
        scratch = Path(settings.media_scratch_path) / f"{file_id}_{uuid4().hex}"
        scratch.mkdir(parents=True, exist_ok=True)
        heartbeat_task = None
        try:
            async with async_session_factory() as db:
                record = await MediaService._get_source(db, file_id, lock=True)
                if not record or not FileService.is_media_source(record):
                    return "ignored"
                if (
                    record.media_processing_status == MediaProcessingStatus.COMPLETED
                    and MediaService.thumbnail_id(record)
                ):
                    return "completed"
                if (
                    record.media_processing_status == MediaProcessingStatus.PROCESSING
                    and record.media_processing_task_id not in {"", celery_task_id}
                ):
                    heartbeat = record.media_processing_heartbeat_at
                    if heartbeat and heartbeat.tzinfo is None:
                        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
                    if heartbeat and (
                        MediaService._utc_now() - heartbeat
                    ).total_seconds() < settings.media_stale_processing_seconds:
                        return "ignored"
                record.media_processing_status = MediaProcessingStatus.PROCESSING
                record.media_processing_task_id = celery_task_id
                record.media_processing_attempts = int(record.media_processing_attempts or 0) + 1
                record.media_processing_heartbeat_at = MediaService._utc_now()
                record.media_processing_error_code = ""
                record.media_processing_error = ""
                await db.commit()
                object_key = record.storage_object.object_key
                suffix = Path(record.original_name or record.filename).suffix or ".bin"
                source_path = scratch / f"source{suffix.lower()}"
                thumbnail_path = scratch / "thumbnail.jpg"
                cover_path = scratch / "cover.png"
                source_size = record.file_size
                source_user_id = record.user_id
                source_type = record.file_type

            heartbeat_task = asyncio.create_task(MediaService._heartbeat(file_id, celery_task_id))
            await get_storage_backend().download_file(object_key, source_path)
            if source_type.value == "video":
                metainfo = await MediaService._video_metadata(source_path, cover_path)
                thumbnail_info = await asyncio.to_thread(
                    MediaService._save_thumbnail, cover_path, thumbnail_path
                )
                metainfo.update(
                    {
                        "thumbnail_width": thumbnail_info["thumbnail_width"],
                        "thumbnail_height": thumbnail_info["thumbnail_height"],
                    }
                )
            else:
                metainfo = await asyncio.to_thread(
                    MediaService._save_thumbnail, source_path, thumbnail_path
                )
            metainfo["size_bytes"] = source_size

            async with async_session_factory() as db:
                record = await MediaService._get_source(db, file_id, lock=True)
                if not record:
                    return "ignored"
                existing_link = await FileService.get_derivative(
                    db, record.id, FileDerivativeVariant.THUMBNAIL
                )
                from app.utils.hash import compute_file_hash

                thumbnail_hash = compute_file_hash(str(thumbnail_path))
                thumbnail_size = thumbnail_path.stat().st_size
                if (
                    existing_link
                    and not existing_link.derivative_file.is_deleted
                    and existing_link.derivative_file.file_hash == thumbnail_hash
                    and existing_link.derivative_file.file_size == thumbnail_size
                ):
                    thumbnail = existing_link.derivative_file
                else:
                    thumbnail = await FileService.create_record_from_path(
                        db=db,
                        user_id=source_user_id,
                        path=thumbnail_path,
                        filename=f"{Path(record.filename).stem}_thumbnail.jpg",
                        category=FileCategory.PREVIEW_IMAGE,
                        mime_type="image/jpeg",
                        enforce_quota=True,
                        metainfo={
                            "width": metainfo["thumbnail_width"],
                            "height": metainfo["thumbnail_height"],
                        },
                    )
                    await FileService.link_derivative(
                        db, record, thumbnail, FileDerivativeVariant.THUMBNAIL
                    )
                record.metainfo = {**(record.metainfo or {}), **metainfo}
                record.media_processing_status = MediaProcessingStatus.COMPLETED
                record.media_processing_error_code = ""
                record.media_processing_error = ""
                record.media_processing_heartbeat_at = MediaService._utc_now()
                record.media_processed_at = MediaService._utc_now()
                await db.commit()
                return thumbnail.public_id
        except QuotaExceededException as exc:
            await MediaService._mark_failed(file_id, "MEDIA_DERIVATIVE_QUOTA_EXCEEDED", str(exc.detail))
            raise MediaProcessingFailure("MEDIA_DERIVATIVE_QUOTA_EXCEEDED", str(exc.detail)) from exc
        except MediaProcessingFailure as exc:
            await MediaService._mark_failed(file_id, exc.code, str(exc))
            raise
        except Exception as exc:
            await MediaService._mark_failed(file_id, "MEDIA_PROCESSING_FAILED", str(exc))
            raise MediaProcessingFailure("MEDIA_PROCESSING_FAILED", str(exc)) from exc
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await heartbeat_task
            shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    async def recover_stale() -> int:
        threshold = MediaService._utc_now() - timedelta(seconds=settings.media_stale_processing_seconds)
        async with async_session_factory() as db:
            records = list(
                (
                    await db.execute(
                        select(FileRecord).options(*FileService.load_options()).where(
                            FileRecord.is_deleted.is_(False),
                            or_(
                                and_(
                                    FileRecord.media_processing_status == MediaProcessingStatus.PENDING,
                                    FileRecord.media_processing_task_id == "dispatching",
                                    FileRecord.media_processing_heartbeat_at < threshold,
                                ),
                                and_(
                                    FileRecord.media_processing_status == MediaProcessingStatus.PENDING,
                                    FileRecord.media_processing_task_id == "",
                                ),
                                and_(
                                    FileRecord.media_processing_status == MediaProcessingStatus.PROCESSING,
                                    FileRecord.media_processing_heartbeat_at < threshold,
                                ),
                            ),
                        )
                    )
                ).scalars()
            )
            for record in records:
                await MediaService.enqueue(db, record, force=True)
            return len(records)
