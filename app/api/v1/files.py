from typing import Optional
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.storage import get_storage_backend
from app.schemas.file import (
    FileDownloadCompleteRequest,
    FileDownloadCompleteResponse,
    FileDownloadInitResponse,
    FileDownloadInitRequest,
    FileDownloadStatusResponse,
    FileResponse,
    FileDeleteResponse,
    FileListResponse,
    MediaProcessingRetryResponse,
)
from app.services.file_service import FileService
from app.services.media_service import MediaService
from app.models.file import FileCategory, FileType
from app.models.user import User
from app.utils.hash import compute_chunk_hash

router = APIRouter(prefix="/files", tags=["files"])


def _content_disposition(filename: str) -> str:
    safe_name = (filename or "download").replace('"', "")
    return f'attachment; filename="{safe_name}"'


@router.get("", response_model=FileListResponse)
async def list_files(
    category: Optional[FileCategory] = Query(None),
    file_type: Optional[FileType] = Query(None),
    include_derivatives: bool = Query(False),
    file_hash: Optional[str] = Query(None, min_length=1, max_length=128),
    file_size: Optional[int] = Query(None, ge=0),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    files, total = await FileService.list_by_user(
        db=db,
        user_id=current_user.id,
        category=category,
        file_type=file_type,
        include_derivatives=include_derivatives,
        file_hash=file_hash,
        file_size=file_size,
        skip=skip,
        limit=limit,
    )
    return FileListResponse(
        files=[FileResponse.from_record(f) for f in files],
        total=total,
    )


@router.get("/downloads/{download_id}/progress", response_model=FileDownloadStatusResponse)
async def get_download_progress(
    download_id: str,
    current_user: User = Depends(get_current_user),
):
    return FileDownloadStatusResponse(
        **await FileService.get_download_session(download_id, current_user.id)
    )


@router.post("/downloads/{download_id}/complete", response_model=FileDownloadCompleteResponse)
async def complete_download(
    download_id: str,
    body: FileDownloadCompleteRequest,
    current_user: User = Depends(get_current_user),
):
    session = await FileService.complete_download_session(
        download_id=download_id,
        user_id=current_user.id,
        expected_hash=body.expected_hash,
        expected_size=body.expected_size,
        parts=[(part.chunk_index, part.etag) for part in body.parts],
    )
    return FileDownloadCompleteResponse(
        download_id=session["download_id"],
        file_id=session["file_id"],
        file_hash=session.get("file_hash", ""),
        file_size=session["file_size"],
        total_chunks=session["total_chunks"],
        downloaded_chunks=session["downloaded_chunks"],
        verified=session["status"] == "completed",
        status=session["status"],
    )


@router.get("/{file_id}", response_model=FileResponse)
async def get_file(
    file_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    file = await FileService.get_accessible_file(db, file_id, current_user)
    return FileResponse.from_record(
        file,
        include_private=current_user.is_admin or file.user_id == current_user.id,
    )


@router.post("/{file_id}/download/init", response_model=FileDownloadInitResponse)
async def init_file_download(
    file_id: str,
    body: Optional[FileDownloadInitRequest] = Body(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return FileDownloadInitResponse(
        **await FileService.create_download_session(
            db,
            file_id,
            current_user,
            body.chunk_size if body else None,
        )
    )


@router.get("/{file_id}/download/chunk")
async def download_file_chunk(
    file_id: str,
    download_id: str = Query(...),
    chunk_index: int = Query(..., ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    file = await FileService.get_accessible_file(db, file_id, current_user)
    session = await FileService.get_download_session(download_id, current_user.id)
    if session.get("file_id") != file.storage_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Download session not found")

    total_chunks = int(session.get("total_chunks") or 0)
    chunk_size = int(session["chunk_size"])
    if chunk_index >= total_chunks:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chunk index")

    start = chunk_index * chunk_size
    end = min(start + chunk_size - 1, int(session.get("file_size") or 0) - 1)
    filename = file.original_name or file.filename
    media_type = file.mime_type or "application/octet-stream"

    data = await get_storage_backend().read_range(file.storage_object.object_key, start, end)

    etag = compute_chunk_hash(data)
    await FileService.record_download_chunk(
        download_id=download_id,
        user_id=current_user.id,
        file_id=file.public_id,
        chunk_index=chunk_index,
        start=start,
        end=end,
        etag=etag,
    )
    file.download_count += 1
    await db.flush()

    return Response(
        content=data,
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(data)),
            "Content-Range": f"bytes {start}-{end}/{session['file_size']}",
            "Content-Disposition": _content_disposition(filename),
            "X-File-Id": file.public_id,
            "X-Download-Id": download_id,
            "X-Chunk-Index": str(chunk_index),
            "X-Chunk-Etag": etag,
            "ETag": f'"{etag}"',
        },
    )


@router.delete("/{file_id}", response_model=FileDeleteResponse)
async def delete_file(
    file_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    file = await FileService.delete_file(db, file_id, current_user)
    return FileDeleteResponse(deleted=True, file_id=file.public_id)


@router.post("/{file_id}/archive", response_model=FileResponse)
async def archive_file(
    file_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    file = await FileService.archive_file(db, file_id, current_user)
    return FileResponse.from_record(file)


@router.post("/{file_id}/media-processing/retry", response_model=MediaProcessingRetryResponse)
async def retry_media_processing(
    file_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    file = await FileService.get_accessible_file(db, file_id, current_user, write=True)
    if not FileService.is_media_source(file):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only original image or video files support media processing",
        )
    await MediaService.enqueue(db, file, force=True)
    return MediaProcessingRetryResponse(
        file_id=file.public_id,
        media_processing_status=file.media_processing_status,
        thumbnail_id=MediaService.thumbnail_id(file),
    )
