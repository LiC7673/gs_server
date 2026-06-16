import pytest
from sqlalchemy import select
from datetime import datetime, timedelta, timezone

from app.models.file import FileRecord, StorageObject
from app.models.task import TaskFileRecord, TaskFileRole, TaskRecord, TaskStatus, TaskVisibility
from app.models.user import User
from app.services.task_service import TaskService
from app.utils.hash import compute_chunk_hash, compute_hash


async def register(client, username):
    response = await client.post("/api/v1/auth/register", json={
        "username": username,
        "email": f"{username}@example.com",
        "password": "testpass123",
    })
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def upload(client, headers, name, content, mime_type="image/png"):
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
    return merged.json()["file_id"]


@pytest.mark.asyncio
async def test_completed_task_visibility_patch_returns_fresh_response(client, db_session):
    owner_headers = await register(client, "visibilityowner")
    result_id = await upload(client, owner_headers, "visibility.ply", b"visibility-result", "model/ply")
    created = await client.post("/api/v1/reconstruction/tasks", headers=owner_headers, json={
        "title": "visibility demo",
        "algorithm": "anysplat",
        "params": {},
    })
    task_id = created.json()["task_id"]
    task = await TaskService.get_by_public_id(db_session, task_id)
    result_file = (await db_session.execute(select(FileRecord).where(FileRecord.public_id == result_id))).scalar_one()
    await TaskService.add_file_link(db_session, task, result_file, TaskFileRole.RESULT)
    task.status = TaskStatus.COMPLETED
    task.current_stage = "gaussian_completed"
    task.progress = 100.0
    await db_session.commit()

    response = await client.patch(
        f"/api/v1/reconstruction/tasks/{task_id}/visibility",
        headers=owner_headers,
        json={"visibility": "public"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == task_id
    assert data["visibility"] == "public"
    assert data["updated_at"]
    assert data["result_id"] == result_id


@pytest.mark.asyncio
async def test_private_task_and_input_are_hidden_but_public_result_is_readable(client, db_session):
    owner_headers = await register(client, "permissionowner")
    other_headers = await register(client, "permissionreader")
    input_id = await upload(client, owner_headers, "input.png", b"private-input")
    result_id = await upload(client, owner_headers, "result.ply", b"public-result", "model/ply")
    created = await client.post("/api/v1/reconstruction/tasks", headers=owner_headers, json={
        "title": "official case",
        "algorithm": "anysplat",
        "params": {},
    })
    task_id = created.json()["task_id"]
    private_read = await client.get(f"/api/v1/reconstruction/tasks/{task_id}", headers=other_headers)
    assert private_read.status_code == 404

    task = await TaskService.get_by_public_id(db_session, task_id)
    input_file = (await db_session.execute(select(FileRecord).where(FileRecord.public_id == input_id))).scalar_one()
    result_file = (await db_session.execute(select(FileRecord).where(FileRecord.public_id == result_id))).scalar_one()
    await TaskService.add_file_link(db_session, task, input_file, TaskFileRole.INPUT)
    await TaskService.add_file_link(db_session, task, result_file, TaskFileRole.RESULT)
    task.status = TaskStatus.COMPLETED
    task.visibility = TaskVisibility.PUBLIC
    await db_session.commit()

    public_task = await client.get(f"/api/v1/reconstruction/tasks/{task_id}", headers=other_headers)
    assert public_task.status_code == 200
    assert public_task.json()["input_file_ids"] == []
    assert public_task.json()["result_id"] == result_id
    hidden_input = await client.get(f"/api/v1/files/{input_id}", headers=other_headers)
    assert hidden_input.status_code == 404
    public_result = await client.get(f"/api/v1/files/{result_id}", headers=other_headers)
    assert public_result.status_code == 200
    discover = await client.get("/api/v1/reconstruction/discover", headers=other_headers)
    assert discover.status_code == 200
    assert discover.json()["tasks"][0]["task_id"] == task_id

    task.status = TaskStatus.PROCESSING
    task.current_stage = "mesh_processing"
    await db_session.commit()
    assert (await client.get(f"/api/v1/reconstruction/tasks/{task_id}", headers=other_headers)).status_code == 200
    assert (await client.get(f"/api/v1/files/{result_id}", headers=other_headers)).status_code == 200
    discover = await client.get("/api/v1/reconstruction/discover", headers=other_headers)
    assert discover.status_code == 200
    assert discover.json()["tasks"][0]["task_id"] == task_id


@pytest.mark.asyncio
async def test_discover_pagination_is_capped_and_legacy_limit_is_validated(client, db_session):
    headers = await register(client, "discoverpager")
    user = (
        await db_session.execute(select(User).where(User.username == "discoverpager"))
    ).scalar_one()
    now = datetime.now(timezone.utc)
    result_id = await upload(client, headers, "discover.ply", b"discover-result", "model/ply")
    result_file = (await db_session.execute(select(FileRecord).where(FileRecord.public_id == result_id))).scalar_one()
    for index in range(12):
        task = TaskRecord(
            user_id=user.id,
            title=f"discover {index}",
            algorithm="anysplat",
            params="{}",
            visibility=TaskVisibility.PUBLIC,
            status=TaskStatus.COMPLETED,
            current_stage="completed",
            progress=100.0,
            input_kind="image_folder",
            completed_at=now - timedelta(minutes=index),
        )
        db_session.add(task)
        await db_session.flush()
        db_session.add(TaskFileRecord(task=task, file=result_file, role=TaskFileRole.RESULT))
    await db_session.commit()

    default_page = await client.get("/api/v1/reconstruction/discover", headers=headers)
    assert default_page.status_code == 200
    default_data = default_page.json()
    assert default_data["total"] == 12
    assert len(default_data["tasks"]) == 10
    assert default_data["page"] == 1
    assert default_data["page_size"] == 10
    assert default_data["total_pages"] == 2
    assert default_data["has_next"] is True
    assert default_data["has_prev"] is False

    second_page = await client.get(
        "/api/v1/reconstruction/discover?page=2&page_size=10",
        headers=headers,
    )
    assert second_page.status_code == 200
    assert len(second_page.json()["tasks"]) == 2
    assert second_page.json()["has_prev"] is True

    legacy = await client.get(
        "/api/v1/reconstruction/discover?skip=0&limit=10",
        headers=headers,
    )
    assert legacy.status_code == 200
    assert legacy.json()["page_size"] == 10

    assert (
        await client.get("/api/v1/reconstruction/discover?page=0&page_size=10", headers=headers)
    ).status_code == 422
    assert (
        await client.get("/api/v1/reconstruction/discover?page=1&page_size=11", headers=headers)
    ).status_code == 422
    assert (
        await client.get("/api/v1/reconstruction/discover?skip=0&limit=50", headers=headers)
    ).status_code == 422


@pytest.mark.asyncio
async def test_delete_public_result_returns_task_to_private(client, db_session):
    owner_headers = await register(client, "deleteowner")
    result_id = await upload(client, owner_headers, "delete.ply", b"delete-result", "model/ply")
    created = await client.post("/api/v1/reconstruction/tasks", headers=owner_headers, json={
        "title": "delete demo",
        "algorithm": "anysplat",
        "params": {},
    })
    task = await TaskService.get_by_public_id(db_session, created.json()["task_id"])
    result_file = (await db_session.execute(select(FileRecord).where(FileRecord.public_id == result_id))).scalar_one()
    await TaskService.add_file_link(db_session, task, result_file, TaskFileRole.RESULT)
    task.status = TaskStatus.COMPLETED
    task.visibility = TaskVisibility.PUBLIC
    await db_session.commit()
    deleted = await client.delete(f"/api/v1/files/{result_id}", headers=owner_headers)
    assert deleted.status_code == 200
    await db_session.refresh(task)
    assert task.visibility == TaskVisibility.PRIVATE


@pytest.mark.asyncio
async def test_same_content_is_deduplicated_per_user_not_globally(client, db_session):
    first_headers = await register(client, "dedupfirst")
    second_headers = await register(client, "dedupsecond")
    content = b"same-content-different-users"
    first_id = await upload(client, first_headers, "same.png", content)
    second_id = await upload(client, second_headers, "same.png", content)
    assert first_id != second_id
    objects = list((await db_session.execute(select(StorageObject))).scalars())
    matching = [item for item in objects if item.file_hash == compute_hash(content)]
    assert len(matching) == 2
    assert matching[0].object_key != matching[1].object_key


@pytest.mark.asyncio
async def test_delete_active_input_cancels_task(client, db_session):
    owner_headers = await register(client, "cancelinput")
    input_id = await upload(client, owner_headers, "cancel.png", b"cancel-input")
    created = await client.post("/api/v1/reconstruction/tasks", headers=owner_headers, json={
        "title": "cancel input",
        "algorithm": "anysplat",
        "params": {},
    })
    task = await TaskService.get_by_public_id(db_session, created.json()["task_id"])
    input_file = (await db_session.execute(select(FileRecord).where(FileRecord.public_id == input_id))).scalar_one()
    await TaskService.add_file_link(db_session, task, input_file, TaskFileRole.INPUT)
    await db_session.commit()
    deleted = await client.delete(f"/api/v1/files/{input_id}", headers=owner_headers)
    assert deleted.status_code == 200
    await db_session.refresh(task)
    assert task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_delete_task_preserves_linked_file(client, db_session):
    owner_headers = await register(client, "deletetask")
    result_id = await upload(client, owner_headers, "keep.ply", b"keep-result", "model/ply")
    created = await client.post("/api/v1/reconstruction/tasks", headers=owner_headers, json={
        "title": "delete task",
        "algorithm": "anysplat",
        "params": {},
    })
    task_id = created.json()["task_id"]
    task = await TaskService.get_by_public_id(db_session, task_id)
    result_file = (await db_session.execute(select(FileRecord).where(FileRecord.public_id == result_id))).scalar_one()
    await TaskService.add_file_link(db_session, task, result_file, TaskFileRole.RESULT)
    await db_session.commit()
    deleted = await client.delete(f"/api/v1/reconstruction/tasks/{task_id}", headers=owner_headers)
    assert deleted.status_code == 200
    assert (await client.get(f"/api/v1/reconstruction/tasks/{task_id}", headers=owner_headers)).status_code == 404
    assert (await client.get(f"/api/v1/files/{result_id}", headers=owner_headers)).status_code == 200
