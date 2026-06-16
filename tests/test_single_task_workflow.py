import pytest

from app.api.v1.reconstruction import _set_stage_failed
from app.models.file import FileCategory
from app.models.task import TaskFileRole, TaskStatus
from app.services.file_service import FileService
from app.services.task_service import TaskService
from app.utils.hash import compute_hash


async def register(client, username):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "testpass123",
        },
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def create_task(client, headers):
    return await client.post(
        "/api/v1/reconstruction/tasks",
        headers=headers,
        json={
            "title": "single workflow",
            "algorithm": "anysplat",
            "params": {"frame_nums": 4},
        },
    )


async def upload_to_task(client, headers, task_id, name, content):
    digest = compute_hash(content)
    init = await client.post(
        "/api/v1/upload/init",
        headers=headers,
        json={
            "task_id": task_id,
            "filename": name,
            "file_size": len(content),
            "chunk_size": len(content),
            "mime_type": "image/png",
            "file_hash": digest,
        },
    )
    upload_id = init.json()["upload_id"]
    chunk = await client.put(
        f"/api/v1/upload/{upload_id}/chunk?chunk_index=0",
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=content,
    )
    merged = await client.post(
        f"/api/v1/upload/{upload_id}/merge",
        headers=headers,
        json={
            "expected_hash": digest,
            "expected_size": len(content),
            "parts": [{"chunk_index": 0, "etag": chunk.json()["etag"]}],
        },
    )
    return merged


@pytest.mark.asyncio
async def test_task_upload_and_gaussian_queue_follow_single_state_chain(client):
    headers = await register(client, "singleworkflow")
    created = await create_task(client, headers)
    assert created.status_code == 200
    body = created.json()
    task_id = body["task_id"]
    assert body["current_stage"] == "task_created"
    assert "mesh_algorithm" not in body
    assert "mesh_params" not in body

    file_ids = []
    for index in range(3):
        merged = await upload_to_task(
            client,
            headers,
            task_id,
            f"image-{index}.png",
            f"workflow-image-{index}".encode(),
        )
        assert merged.status_code == 200
        assert merged.json()["task_id"] == task_id
        file_ids.append(merged.json()["file_id"])

    uploading = await client.get(f"/api/v1/reconstruction/status/{task_id}", headers=headers)
    assert uploading.json()["current_stage"] == "data_uploading"
    assert uploading.json()["mesh_algorithm"] is None
    assert set(uploading.json()["input_file_ids"]) == set(file_ids)

    started = await client.post(
        f"/api/v1/reconstruction/start/{task_id}",
        headers=headers,
        json={},
    )
    assert started.status_code == 200
    assert started.json()["current_stage"] == "gaussian_queued"
    assert started.json()["input_file_count"] == 3


@pytest.mark.asyncio
async def test_gaussian_result_waits_for_manual_mesh_start_on_same_task(
    client,
    db_session,
    fake_infrastructure,
):
    headers = await register(client, "automeshworkflow")
    created = await create_task(client, headers)
    task_id = created.json()["task_id"]
    task = await TaskService.get_by_public_id(db_session, task_id)

    storage_object, _ = await FileService.get_or_create_storage_object(
        db_session,
        task.user_id,
        "a" * 64,
        9,
    )
    ply = await FileService.create_record(
        db=db_session,
        user_id=task.user_id,
        filename="point_cloud.ply",
        original_name="point_cloud.ply",
        category=FileCategory.PLY_MODEL,
        storage_object=storage_object,
        file_size=9,
        mime_type="model/ply",
        file_hash="a" * 64,
        metainfo={
            "generated_by": "dash_gaussian",
            "generation_id": "dash_gaussian_workflow",
            "relative_path": "point_cloud/iteration_30000/point_cloud.ply",
            "primary_result": True,
        },
    )
    cfg_storage_object, _ = await FileService.get_or_create_storage_object(
        db_session,
        task.user_id,
        "c" * 64,
        8,
    )
    cfg = await FileService.create_record(
        db=db_session,
        user_id=task.user_id,
        filename="cfg_args",
        original_name="cfg_args",
        category=FileCategory.OTHER,
        storage_object=cfg_storage_object,
        file_size=8,
        mime_type="text/plain",
        file_hash="c" * 64,
        metainfo={
            "generated_by": "dash_gaussian",
            "generation_id": "dash_gaussian_workflow",
            "relative_path": "cfg_args",
            "primary_result": False,
        },
    )
    await TaskService.add_file_link(db_session, task, ply, TaskFileRole.RESULT)
    await TaskService.add_file_link(db_session, task, cfg, TaskFileRole.RESULT)
    task.status = TaskStatus.COMPLETED
    task.current_stage = "gaussian_completed"
    await db_session.commit()

    status = await client.get(f"/api/v1/reconstruction/status/{task_id}", headers=headers)
    assert status.json()["status"] == "completed"
    assert status.json()["current_stage"] == "gaussian_completed"
    assert fake_infrastructure.queued_tasks == []

    started = await client.post(
        f"/api/v1/reconstruction/mesh/start/{task_id}",
        headers=headers,
        json={
            "algorithm": "dash_gaussian_mesh",
            "input_file_ids": [ply.public_id],
            "params": {"radius": 10},
        },
    )
    assert started.status_code == 200

    refreshed = await TaskService.get_by_public_id(db_session, task_id)
    assert refreshed.public_id == task_id
    assert refreshed.algorithm == "dash_gaussian_mesh"
    assert refreshed.status == TaskStatus.QUEUED
    assert refreshed.current_stage == "mesh_queued"
    assert refreshed.input_kind == "ply_model"
    assert refreshed.progress == 0
    assert any(
        link.role == TaskFileRole.RESULT and link.file.public_id == ply.public_id
        for link in refreshed.file_links
    )
    assert fake_infrastructure.queued_tasks[-1]["name"] == "reconstruction.run"


@pytest.mark.asyncio
async def test_mesh_cannot_create_separate_task(client):
    headers = await register(client, "noseparatemesh")
    response = await client.post(
        "/api/v1/reconstruction/tasks",
        headers=headers,
        json={"title": "invalid mesh task", "algorithm": "dash_gaussian_mesh", "params": {}},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_generic_start_reruns_saved_gaussian_after_mesh_stage(client, db_session):
    headers = await register(client, "rerungaussian")
    created = await create_task(client, headers)
    task_id = created.json()["task_id"]
    for index in range(3):
        await upload_to_task(client, headers, task_id, f"rerun-{index}.png", f"rerun-{index}".encode())

    task = await TaskService.get_by_public_id(db_session, task_id)
    task.algorithm = "dash_gaussian_mesh"
    task.params = '{"radius": 10}'
    task.status = TaskStatus.COMPLETED
    task.current_stage = "mesh_completed"
    await db_session.commit()

    started = await client.post(f"/api/v1/reconstruction/start/{task_id}", headers=headers, json={})
    assert started.status_code == 200
    assert started.json()["algorithm"] == "anysplat"
    assert started.json()["current_stage"] == "gaussian_queued"


@pytest.mark.asyncio
async def test_stage_failure_preserves_existing_ply(client, db_session):
    headers = await register(client, "preserveply")
    task_id = (await create_task(client, headers)).json()["task_id"]
    task = await TaskService.get_by_public_id(db_session, task_id)
    storage_object, _ = await FileService.get_or_create_storage_object(db_session, task.user_id, "b" * 64, 9)
    ply = await FileService.create_record(
        db=db_session,
        user_id=task.user_id,
        filename="existing.ply",
        original_name="existing.ply",
        category=FileCategory.PLY_MODEL,
        storage_object=storage_object,
        file_size=9,
        mime_type="model/ply",
        file_hash="b" * 64,
    )
    await TaskService.add_file_link(db_session, task, ply, TaskFileRole.RESULT)

    task.algorithm = "anysplat"
    _set_stage_failed(task, "TEST_GAUSSIAN_FAILURE", "failed")
    assert task.status == TaskStatus.PARTIAL_COMPLETED
    assert task.current_stage == "gaussian_failed"

    task.algorithm = "dash_gaussian_mesh"
    _set_stage_failed(task, "TEST_MESH_FAILURE", "failed")
    assert task.status == TaskStatus.PARTIAL_COMPLETED
    assert task.current_stage == "mesh_failed"
