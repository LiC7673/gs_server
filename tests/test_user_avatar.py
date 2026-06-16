import pytest
from sqlalchemy import select

from app.models.file import FileCategory, FileDerivativeRecord, FileDerivativeVariant, FileRecord
from app.utils.hash import compute_chunk_hash, compute_hash


async def register(client, username):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "testpass123",
        },
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def upload(client, headers, name, content, mime_type):
    file_hash = compute_hash(content)
    init = await client.post(
        "/api/v1/upload/init",
        headers=headers,
        json={
            "filename": name,
            "file_size": len(content),
            "chunk_size": len(content),
            "mime_type": mime_type,
            "file_hash": file_hash,
        },
    )
    assert init.status_code == 200
    upload_id = init.json()["upload_id"]
    chunk = await client.put(
        f"/api/v1/upload/{upload_id}/chunk?chunk_index=0",
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=content,
    )
    assert chunk.status_code == 200
    merged = await client.post(
        f"/api/v1/upload/{upload_id}/merge",
        headers=headers,
        json={
            "expected_hash": file_hash,
            "expected_size": len(content),
            "parts": [{"chunk_index": 0, "etag": compute_chunk_hash(content)}],
        },
    )
    assert merged.status_code == 200
    return merged.json()["file_id"]


@pytest.mark.asyncio
async def test_user_avatar_uses_uploaded_image_and_returns_thumbnail(client, db_session):
    headers = await register(client, "avatar_user")
    profile = (await client.get("/api/v1/users/me", headers=headers)).json()
    assert set(profile) == {
        "id",
        "username",
        "email",
        "nickname",
        "is_admin",
        "avatar_file_id",
        "avatar_thumbnail_file_id",
        "created_at",
    }
    assert profile["avatar_file_id"] is None
    assert profile["avatar_thumbnail_file_id"] is None

    avatar_id = await upload(client, headers, "avatar.png", b"avatar-image", "image/png")
    profile_updated = await client.put(
        "/api/v1/users/me",
        headers=headers,
        json={"nickname": "Avatar User"},
    )
    assert profile_updated.status_code == 200
    assert profile_updated.json()["avatar_file_id"] is None

    mixed_update = await client.put(
        "/api/v1/users/me",
        headers=headers,
        json={"avatar_file_id": avatar_id},
    )
    assert mixed_update.status_code == 422

    updated = await client.put(
        "/api/v1/users/update_avatar",
        headers=headers,
        json={"avatar_file_id": avatar_id},
    )
    assert updated.status_code == 200
    assert set(updated.json()) == {
        "avatar_file_id",
        "avatar_thumbnail_file_id",
        "created_at",
    }
    assert updated.json()["avatar_file_id"] == avatar_id
    assert updated.json()["avatar_thumbnail_file_id"] is None

    thumbnail_id = await upload(client, headers, "avatar-thumb.jpg", b"avatar-thumb", "image/jpeg")
    records = (
        await db_session.execute(
            select(FileRecord).where(FileRecord.public_id.in_([avatar_id, thumbnail_id]))
        )
    ).scalars().all()
    source = next(record for record in records if record.public_id == avatar_id)
    thumbnail = next(record for record in records if record.public_id == thumbnail_id)
    thumbnail.category = FileCategory.PREVIEW_IMAGE
    db_session.add(
        FileDerivativeRecord(
            source_file=source,
            derivative_file=thumbnail,
            variant=FileDerivativeVariant.THUMBNAIL,
        )
    )
    await db_session.flush()

    profile = await client.get("/api/v1/users/me", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["avatar_file_id"] == avatar_id
    assert profile.json()["avatar_thumbnail_file_id"] == thumbnail_id

    rejected_thumbnail = await client.put(
        "/api/v1/users/update_avatar",
        headers=headers,
        json={"avatar_file_id": thumbnail_id},
    )
    assert rejected_thumbnail.status_code == 400

    cleared = await client.put(
        "/api/v1/users/update_avatar",
        headers=headers,
        json={"avatar_file_id": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["avatar_file_id"] is None
    assert cleared.json()["avatar_thumbnail_file_id"] is None


@pytest.mark.asyncio
async def test_user_avatar_rejects_non_image_and_other_users_file(client):
    headers = await register(client, "avatar_owner")
    other_headers = await register(client, "avatar_other")

    text_id = await upload(client, headers, "notes.json", b"{}", "application/json")
    non_image = await client.put(
        "/api/v1/users/update_avatar",
        headers=headers,
        json={"avatar_file_id": text_id},
    )
    assert non_image.status_code == 400

    other_image_id = await upload(client, other_headers, "other.png", b"other-image", "image/png")
    other_file = await client.put(
        "/api/v1/users/update_avatar",
        headers=headers,
        json={"avatar_file_id": other_image_id},
    )
    assert other_file.status_code == 404
