import pytest
from app.utils.hash import compute_chunk_hash, compute_hash


@pytest.fixture
async def auth_headers(client):
    await client.post("/api/v1/auth/register", json={
        "username": "uploaduser",
        "email": "upload@example.com",
        "password": "testpass123",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "username": "uploaduser",
        "password": "testpass123",
    })
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_init_upload(client, auth_headers):
    resp = await client.post("/api/v1/upload/init", json={
        "filename": "test_video.mp4",
        "file_size": 10485760,
        "mime_type": "video/mp4",
        "file_hash": "0" * 64,
    }, headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "upload_id" in data
    assert data["chunk_size"] == 5242880
    assert data["total_chunks"] == 2


@pytest.mark.asyncio
async def test_upload_chunk(client, auth_headers):
    content = b"upload-chunk-testdata123"
    init = await client.post("/api/v1/upload/init", json={
        "filename": "chunk_test.mp4",
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": "video/mp4",
        "file_hash": compute_hash(content),
    }, headers=auth_headers)
    upload_id = init.json()["upload_id"]
    resp = await client.put(
        f"/api/v1/upload/{upload_id}/chunk?chunk_index=0",
        content=content,
        headers={**auth_headers, "Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200
    assert resp.json()["received"] is True
    assert resp.json()["etag"] == compute_chunk_hash(content)


@pytest.mark.asyncio
async def test_upload_progress(client, auth_headers):
    content = b"upload-progress-testdata123"
    init = await client.post("/api/v1/upload/init", json={
        "filename": "progress_test.mp4",
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": "video/mp4",
        "file_hash": compute_hash(content),
    }, headers=auth_headers)
    upload_id = init.json()["upload_id"]
    await client.put(
        f"/api/v1/upload/{upload_id}/chunk?chunk_index=0",
        content=content,
        headers={**auth_headers, "Content-Type": "application/octet-stream"},
    )
    resp = await client.get(f"/api/v1/upload/{upload_id}/progress", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["received_chunks"] == 1
    assert body["chunk_statuses"] == [2]
    assert "chunk_status_meaning" not in body


@pytest.mark.asyncio
async def test_merge_upload(client, auth_headers):
    content = b"upload-merge-testdata123"
    init = await client.post("/api/v1/upload/init", json={
        "filename": "merge_test.mp4",
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": "video/mp4",
        "file_hash": compute_hash(content),
    }, headers=auth_headers)
    upload_id = init.json()["upload_id"]
    upload_resp = await client.put(
        f"/api/v1/upload/{upload_id}/chunk?chunk_index=0",
        content=content,
        headers={**auth_headers, "Content-Type": "application/octet-stream"},
    )
    etag = upload_resp.json()["etag"]
    resp = await client.post(
        f"/api/v1/upload/{upload_id}/merge",
        json={
            "expected_hash": compute_chunk_hash(content),
            "expected_size": len(content),
            "parts": [{"chunk_index": 0, "etag": etag}],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "file_id" in body
    assert body["file_hash"] == compute_hash(content)
    assert body["verified"] is True
    assert body["already_uploaded"] is False


@pytest.mark.asyncio
async def test_init_duplicate_upload_returns_existing_file(client, auth_headers):
    content = b"duplicate-file-content"
    file_hash = compute_hash(content)
    init = await client.post("/api/v1/upload/init", json={
        "filename": "duplicate.mp4",
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": "video/mp4",
        "file_hash": file_hash,
    }, headers=auth_headers)
    upload_id = init.json()["upload_id"]
    upload_resp = await client.put(
        f"/api/v1/upload/{upload_id}/chunk?chunk_index=0",
        content=content,
        headers={**auth_headers, "Content-Type": "application/octet-stream"},
    )
    etag = upload_resp.json()["etag"]
    merge = await client.post(
        f"/api/v1/upload/{upload_id}/merge",
        json={
            "expected_hash": file_hash,
            "expected_size": len(content),
            "parts": [{"chunk_index": 0, "etag": etag}],
        },
        headers=auth_headers,
    )
    first_file_id = merge.json()["file_id"]

    duplicate = await client.post("/api/v1/upload/init", json={
        "filename": "duplicate-renamed.mp4",
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": "video/mp4",
        "file_hash": file_hash,
    }, headers=auth_headers)
    assert duplicate.status_code == 200
    body = duplicate.json()
    assert body["already_uploaded"] is True
    assert body["upload_id"]
    assert body["total_chunks"] == 0
    assert body["file_id"] == first_file_id
    assert body["storage_key"] == first_file_id
    assert body["file_hash"] == file_hash


@pytest.mark.asyncio
async def test_init_duplicate_image_returns_existing_image_id(client, auth_headers):
    content = b"\x89PNG\r\n\x1a\nsame-image-content"
    file_hash = compute_hash(content)
    init = await client.post("/api/v1/upload/init", json={
        "filename": "duplicate.png",
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": "image/png",
        "file_hash": file_hash,
    }, headers=auth_headers)
    upload_id = init.json()["upload_id"]
    upload_resp = await client.put(
        f"/api/v1/upload/{upload_id}/chunk?chunk_index=0",
        content=content,
        headers={**auth_headers, "Content-Type": "application/octet-stream"},
    )
    merge = await client.post(
        f"/api/v1/upload/{upload_id}/merge",
        json={
            "expected_hash": file_hash,
            "expected_size": len(content),
            "parts": [{"chunk_index": 0, "etag": upload_resp.json()["etag"]}],
        },
        headers=auth_headers,
    )
    first_image_id = merge.json()["image_id"]

    duplicate = await client.post("/api/v1/upload/init", json={
        "filename": "duplicate-renamed.png",
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": "image/png",
        "file_hash": file_hash,
    }, headers=auth_headers)
    assert duplicate.status_code == 200
    body = duplicate.json()
    assert body["already_uploaded"] is True
    assert body["upload_id"]
    assert body["file_id"] == first_image_id
    assert body["image_id"] == first_image_id


@pytest.mark.asyncio
async def test_merge_duplicate_image_returns_existing_image_id(client, auth_headers):
    content = b"\x89PNG\r\n\x1a\nconcurrent-same-image-content"
    file_hash = compute_hash(content)

    async def init_and_upload(filename):
        init = await client.post("/api/v1/upload/init", json={
            "filename": filename,
            "file_size": len(content),
            "chunk_size": len(content),
            "mime_type": "image/png",
            "file_hash": file_hash,
        }, headers=auth_headers)
        upload_id = init.json()["upload_id"]
        upload_resp = await client.put(
            f"/api/v1/upload/{upload_id}/chunk?chunk_index=0",
            content=content,
            headers={**auth_headers, "Content-Type": "application/octet-stream"},
        )
        return upload_id, upload_resp.json()["etag"]

    first_upload_id, first_etag = await init_and_upload("same-a.png")
    second_upload_id, second_etag = await init_and_upload("same-b.png")

    first_merge = await client.post(
        f"/api/v1/upload/{first_upload_id}/merge",
        json={
            "expected_hash": file_hash,
            "expected_size": len(content),
            "parts": [{"chunk_index": 0, "etag": first_etag}],
        },
        headers=auth_headers,
    )
    first_body = first_merge.json()

    second_merge = await client.post(
        f"/api/v1/upload/{second_upload_id}/merge",
        json={
            "expected_hash": file_hash,
            "expected_size": len(content),
            "parts": [{"chunk_index": 0, "etag": second_etag}],
        },
        headers=auth_headers,
    )
    assert second_merge.status_code == 200
    second_body = second_merge.json()
    assert second_body["already_uploaded"] is True
    assert second_body["file_id"] == first_body["file_id"]
    assert second_body["image_id"] == first_body["image_id"]


@pytest.mark.asyncio
async def test_merge_retry_completed_upload_returns_existing_file(client, auth_headers):
    content = b"retry-same-upload-merge"
    file_hash = compute_hash(content)
    init = await client.post("/api/v1/upload/init", json={
        "filename": "retry.mp4",
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": "video/mp4",
        "file_hash": file_hash,
    }, headers=auth_headers)
    upload_id = init.json()["upload_id"]
    upload_resp = await client.put(
        f"/api/v1/upload/{upload_id}/chunk?chunk_index=0",
        content=content,
        headers={**auth_headers, "Content-Type": "application/octet-stream"},
    )
    merge_body = {
        "expected_hash": file_hash,
        "expected_size": len(content),
        "parts": [{"chunk_index": 0, "etag": upload_resp.json()["etag"]}],
    }
    first_merge = await client.post(
        f"/api/v1/upload/{upload_id}/merge",
        json=merge_body,
        headers=auth_headers,
    )
    second_merge = await client.post(
        f"/api/v1/upload/{upload_id}/merge",
        json=merge_body,
        headers=auth_headers,
    )
    assert second_merge.status_code == 200
    assert second_merge.json()["already_uploaded"] is True
    assert second_merge.json()["file_id"] == first_merge.json()["file_id"]


@pytest.mark.asyncio
async def test_cancel_upload(client, auth_headers):
    init = await client.post("/api/v1/upload/init", json={
        "filename": "cancel_test.mp4",
        "file_size": 100,
        "chunk_size": 50,
        "mime_type": "video/mp4",
        "file_hash": "1" * 64,
    }, headers=auth_headers)
    upload_id = init.json()["upload_id"]
    resp = await client.post(f"/api/v1/upload/{upload_id}/cancel", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True
