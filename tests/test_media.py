from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.models.file import FileRecord, MediaProcessingStatus
from app.services.media_service import MediaService
from app.utils.hash import compute_chunk_hash, compute_hash


async def register(client, username):
    response = await client.post("/api/v1/auth/register", json={
        "username": username,
        "email": f"{username}@example.com",
        "password": "testpass123",
    })
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def upload(client, headers, name, content, mime_type):
    file_hash = compute_hash(content)
    init = await client.post("/api/v1/upload/init", headers=headers, json={
        "filename": name,
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": mime_type,
        "file_hash": file_hash,
    })
    upload_id = init.json()["upload_id"]
    chunk = await client.put(
        f"/api/v1/upload/{upload_id}/chunk?chunk_index=0",
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=content,
    )
    merged = await client.post(f"/api/v1/upload/{upload_id}/merge", headers=headers, json={
        "expected_hash": file_hash,
        "expected_size": len(content),
        "parts": [{"chunk_index": 0, "etag": compute_chunk_hash(content)}],
    })
    assert chunk.status_code == 200
    assert merged.status_code == 200
    return merged.json()


@pytest.mark.asyncio
async def test_image_thumbnail_metadata_list_download_and_delete(
    client, db_session, engine, fake_infrastructure, tmp_path, monkeypatch
):
    image = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(settings, "media_scratch_path", str(tmp_path))
    monkeypatch.setattr(
        "app.services.media_service.async_session_factory",
        async_sessionmaker(engine, expire_on_commit=False),
    )
    source_path = tmp_path / "source.png"
    image.new("RGB", (1024, 256), color=(20, 80, 140)).save(source_path)
    headers = await register(client, "mediaimage")

    merged = await upload(client, headers, "source.png", source_path.read_bytes(), "image/png")
    source_id = merged["file_id"]
    assert merged["media_processing_status"] == "pending"
    assert merged["thumbnail_id"] is None
    assert fake_infrastructure.queued_tasks[-1]["name"] == "media.process"

    thumbnail_id = await MediaService.process(source_id, "manual-test")
    detail = await client.get(f"/api/v1/files/{source_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["file_type"] == "image"
    assert body["media_processing_status"] == "completed"
    assert body["thumbnail_id"] == thumbnail_id
    assert body["metainfo"]["width"] == 1024
    assert body["metainfo"]["height"] == 256
    assert body["metainfo"]["thumbnail_width"] == 512
    assert body["metainfo"]["thumbnail_height"] == 128

    default_list = await client.get("/api/v1/files", headers=headers)
    expanded_list = await client.get("/api/v1/files?include_derivatives=true", headers=headers)
    assert default_list.json()["total"] == 1
    assert expanded_list.json()["total"] == 2

    thumbnail = await client.get(f"/api/v1/files/{thumbnail_id}", headers=headers)
    assert thumbnail.json()["source_file_id"] == source_id
    assert thumbnail.json()["derivative_type"] == "thumbnail"
    assert thumbnail.json()["category"] == "preview_image"
    init_download = await client.post(f"/api/v1/files/{thumbnail_id}/download/init", headers=headers)
    download = init_download.json()
    part = await client.get(
        f"/api/v1/files/{thumbnail_id}/download/chunk",
        params={"download_id": download["download_id"], "chunk_index": 0},
        headers=headers,
    )
    assert part.status_code == 206
    assert part.content.startswith(b"\xff\xd8")

    deleted = await client.delete(f"/api/v1/files/{source_id}", headers=headers)
    assert deleted.status_code == 200
    assert (await client.get(f"/api/v1/files/{thumbnail_id}", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_video_thumbnail_uses_media_metadata(client, engine, tmp_path, monkeypatch):
    image = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(settings, "media_scratch_path", str(tmp_path))
    monkeypatch.setattr(
        "app.services.media_service.async_session_factory",
        async_sessionmaker(engine, expire_on_commit=False),
    )
    headers = await register(client, "mediavideo")
    merged = await upload(client, headers, "sample.mkv", b"fake-video", "video/x-matroska")

    async def fake_video_metadata(source_path: Path, cover_path: Path):
        image.new("RGB", (1920, 1080), color=(80, 10, 10)).save(cover_path)
        return {
            "width": 1920,
            "height": 1080,
            "duration_seconds": 12.34,
            "fps": 29.97,
            "frame_count": 370,
            "codec_name": "h264",
            "bit_rate": 8000000,
        }

    monkeypatch.setattr(MediaService, "_video_metadata", fake_video_metadata)
    thumbnail_id = await MediaService.process(merged["file_id"], "manual-video-test")
    detail = await client.get(f"/api/v1/files/{merged['file_id']}", headers=headers)
    body = detail.json()
    assert body["file_type"] == "video"
    assert body["category"] == "original_video"
    assert body["thumbnail_id"] == thumbnail_id
    assert body["metainfo"]["fps"] == 29.97
    assert body["metainfo"]["duration_seconds"] == 12.34


@pytest.mark.asyncio
async def test_manual_retry_requeues_failed_media(client, db_session):
    headers = await register(client, "mediaretry")
    merged = await upload(client, headers, "retry.png", b"invalid-image", "image/png")
    source = (
        await db_session.execute(select(FileRecord).where(FileRecord.public_id == merged["file_id"]))
    ).scalar_one()
    source.media_processing_status = MediaProcessingStatus.FAILED
    source.media_processing_error_code = "MEDIA_PROCESSING_FAILED"
    source.media_processing_error = "broken"
    await db_session.commit()

    response = await client.post(
        f"/api/v1/files/{source.public_id}/media-processing/retry",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["media_processing_status"] == "pending"
