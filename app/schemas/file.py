from datetime import datetime
import re
from typing import Any, Dict, Optional, List
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import inspect
from sqlalchemy.orm.attributes import NO_VALUE
from app.models.file import FileCategory, FileDerivativeVariant, FileType, MediaProcessingStatus


HEX_32_RE = re.compile(r"^[a-f0-9]{32}$")
HEX_64_RE = re.compile(r"^[a-f0-9]{64}$")


class FileDerivativeResponse(BaseModel):
    type: FileDerivativeVariant
    file_id: str


class FileResponse(BaseModel):
    id: str
    user_id: int
    task_id: Optional[str] = None
    filename: str
    original_name: str
    category: FileCategory
    file_type: FileType
    mime_type: str
    file_size: int
    file_hash: str
    metainfo: Dict[str, Any] = Field(default_factory=dict)
    media_processing_status: MediaProcessingStatus
    media_processing_error_code: Optional[str] = None
    media_processing_error: Optional[str] = None
    source_file_id: Optional[str] = None
    derivative_type: Optional[FileDerivativeVariant] = None
    thumbnail_id: Optional[str] = None
    derivatives: List[FileDerivativeResponse] = Field(default_factory=list)
    storage_key: str
    is_archived: bool
    download_count: int
    created_at: datetime

    @classmethod
    def from_record(cls, value, *, include_private: bool = True):
        source_link = _loaded_scalar(value, "source_link")
        source_file = _loaded_scalar(source_link, "source_file") if source_link else None
        derivative_links = []
        for link in _loaded_collection(value, "derivatives"):
            derivative_file = _loaded_scalar(link, "derivative_file")
            if derivative_file and not derivative_file.is_deleted:
                derivative_links.append((link, derivative_file))
        thumbnail = next(
            (
                derivative_file.public_id
                for link, derivative_file in derivative_links
                if link.variant == FileDerivativeVariant.THUMBNAIL
            ),
            None,
        )
        return cls(**{
            "id": value.storage_key,
            "user_id": value.user_id,
            "task_id": None,
            "filename": value.filename,
            "original_name": value.original_name,
            "category": value.category,
            "file_type": value.file_type,
            "mime_type": value.mime_type,
            "file_size": value.file_size,
            "file_hash": value.file_hash,
            "metainfo": {"size_bytes": value.file_size, **(value.metainfo or {})},
            "media_processing_status": value.media_processing_status,
            "media_processing_error_code": (value.media_processing_error_code or None) if include_private else None,
            "media_processing_error": (value.media_processing_error or None) if include_private else None,
            "source_file_id": source_file.public_id if source_file else None,
            "derivative_type": source_link.variant if source_link else None,
            "thumbnail_id": thumbnail,
            "derivatives": [
                FileDerivativeResponse(type=link.variant, file_id=derivative_file.public_id)
                for link, derivative_file in derivative_links
            ],
            "storage_key": value.storage_key,
            "is_archived": value.is_archived,
            "download_count": value.download_count,
            "created_at": value.created_at,
        })


def _loaded_scalar(value, attr_name: str):
    loaded = inspect(value).attrs[attr_name].loaded_value
    return None if loaded is NO_VALUE else loaded


def _loaded_collection(value, attr_name: str):
    loaded = inspect(value).attrs[attr_name].loaded_value
    return [] if loaded is NO_VALUE else list(loaded or [])


class MediaProcessingRetryResponse(BaseModel):
    file_id: str
    media_processing_status: MediaProcessingStatus
    thumbnail_id: Optional[str] = None


class FileDeleteResponse(BaseModel):
    deleted: bool
    file_id: str
    status: str = "pending_cleanup"


class FileDownloadInitRequest(BaseModel):
    chunk_size: Optional[int] = Field(None, gt=0)


class FileDownloadInitResponse(BaseModel):
    download_id: str
    file_id: str
    filename: str
    mime_type: str
    file_size: int
    file_hash: str
    chunk_size: int
    total_chunks: int
    downloaded_chunks: int
    downloaded_bytes: int
    progress: float
    status: str
    chunk_statuses: List[int]
    created_at: str
    updated_at: str


class FileDownloadStatusResponse(BaseModel):
    download_id: str
    file_id: str
    filename: str
    mime_type: str
    file_size: int
    file_hash: str
    chunk_size: int
    total_chunks: int
    downloaded_chunks: int
    downloaded_bytes: int
    progress: float
    status: str
    chunk_statuses: List[int]
    downloaded_ranges: List[List[int]]
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None


class FileDownloadPart(BaseModel):
    chunk_index: int = Field(..., ge=0)
    etag: str

    @field_validator("etag")
    @classmethod
    def validate_etag(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not HEX_32_RE.fullmatch(normalized):
            raise ValueError("etag must be a chunk MD5 hex string")
        return normalized


class FileDownloadCompleteRequest(BaseModel):
    expected_hash: str = ""
    expected_size: int = Field(0, ge=0)
    parts: List[FileDownloadPart]

    @field_validator("expected_hash")
    @classmethod
    def validate_expected_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and not HEX_64_RE.fullmatch(normalized):
            raise ValueError("expected_hash must be a SHA-256 hex string")
        return normalized


class FileDownloadCompleteResponse(BaseModel):
    download_id: str
    file_id: str
    file_hash: str
    file_size: int
    total_chunks: int
    downloaded_chunks: int
    verified: bool
    status: str


class FileListResponse(BaseModel):
    files: List[FileResponse]
    total: int
