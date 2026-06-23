import pytest
from sqlalchemy import select

from app.api.v1.reconstruction import (
    _algorithm_specs,
    _build_command,
    _register_dash_gaussian_mesh_results,
    _register_hunyuan3d_results,
)
from app.models.file import FileCategory, FileDerivativeRecord, FileDerivativeVariant, FileRecord
from app.models.task import TaskFileRole, TaskStatus
from app.services.file_service import FileService
from app.services.task_service import TaskService
from app.utils.hash import compute_chunk_hash, compute_hash


async def register(client, username):
    response = await client.post("/api/v1/auth/register", json={
        "username": username,
        "email": f"{username}@example.com",
        "password": "testpass123",
    })
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def upload(client, headers, name, content, mime_type="image/png", task_id=None):
    file_hash = compute_hash(content)
    payload = {
        "filename": name,
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": mime_type,
        "file_hash": file_hash,
    }
    if task_id:
        payload["task_id"] = task_id
    init = await client.post("/api/v1/upload/init", headers=headers, json=payload)
    upload_id = init.json()["upload_id"]
    await client.put(
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


async def create_task(client, headers, algorithm="dash_gaussian"):
    response = await client.post("/api/v1/reconstruction/tasks", headers=headers, json={
        "title": f"{algorithm} test",
        "algorithm": algorithm,
        "params": {},
    })
    assert response.status_code == 200
    return response.json()["task_id"]


async def prepare_mesh_task(client, headers, db_session, inputs):
    task_id = await create_task(client, headers)
    input_ids = [
        await upload(client, headers, name, content, mime_type, task_id=task_id)
        for name, content, mime_type in inputs
    ]
    ply_id = await upload(client, headers, "gaussian-result.ply", b"hunyuan-ply", "model/ply")
    task = await TaskService.get_by_public_id(db_session, task_id)
    ply = await FileService.get_by_identifier_for_user(db_session, ply_id, task.user_id)
    await TaskService.add_file_link(db_session, task, ply, TaskFileRole.RESULT)
    task.status = TaskStatus.COMPLETED
    task.current_stage = "gaussian_completed"
    await db_session.commit()
    return task_id, input_ids, ply_id


@pytest.mark.asyncio
async def test_hunyuan3d_is_only_listed_as_mesh_and_cannot_create_task(client):
    headers = await register(client, "hunyuanoneimage")
    algorithms = await client.get("/api/v1/reconstruction/algorithms", headers=headers)
    assert algorithms.status_code == 200
    assert "hunyuan3d" not in {item["name"] for item in algorithms.json()["algorithms"]}
    mesh_algorithms = await client.get("/api/v1/reconstruction/mesh/algorithms", headers=headers)
    hunyuan = next(item for item in mesh_algorithms.json()["algorithms"] if item["name"] == "hunyuan3d")
    assert hunyuan["available"] is True
    assert hunyuan["params"] == []
    assert hunyuan["dependencies"] == {
        "required_stage": "gaussian_completed",
        "required_gaussian_algorithms": [],
        "required_input_type": "original_media",
        "description": "需要任务已有高斯结果，输入使用该任务的原始图片、图片组或单视频。",
    }
    rejected = await client.post("/api/v1/reconstruction/tasks", headers=headers, json={
        "title": "invalid standalone Hunyuan",
        "algorithm": "hunyuan3d",
        "params": {},
    })
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_hunyuan3d_unified_mesh_start_accepts_original_inputs(client, db_session):
    headers = await register(client, "hunyuaninputs")
    task_id, image_ids, _ = await prepare_mesh_task(
        client,
        headers=headers,
        db_session=db_session,
        inputs=[
            ("first.png", b"hunyuan-first-image", "image/png"),
            ("second.png", b"hunyuan-second-image", "image/png"),
        ],
    )
    started = await client.post(
        f"/api/v1/reconstruction/mesh/start/{task_id}",
        headers=headers,
        json={"algorithm": "hunyuan3d", "input_file_ids": image_ids, "params": {}},
    )
    assert started.status_code == 200
    assert started.json()["algorithm"] == "hunyuan3d"
    assert started.json()["input_type"] == "image_folder"
    assert started.json()["input_file_count"] == 2
    status = await client.get(f"/api/v1/reconstruction/status/{task_id}", headers=headers)
    assert status.json()["mesh_algorithm"] == "hunyuan3d"
    assert status.json()["input_file_ids"] == image_ids


@pytest.mark.asyncio
async def test_hunyuan3d_unified_mesh_start_accepts_original_video(client, db_session):
    headers = await register(client, "hunyuanvideo")
    task_id, video_ids, _ = await prepare_mesh_task(
        client,
        headers,
        db_session,
        [("input.mp4", b"hunyuan-video", "video/mp4")],
    )
    started = await client.post(
        f"/api/v1/reconstruction/mesh/start/{task_id}",
        headers=headers,
        json={"algorithm": "hunyuan3d", "input_file_ids": video_ids, "params": {}},
    )
    assert started.status_code == 200
    assert started.json()["input_type"] == "video"


@pytest.mark.asyncio
async def test_hunyuan3d_rejects_mixed_input_and_old_start_route_is_removed(client, db_session):
    headers = await register(client, "hunyuanfolder")
    task_id, input_ids, _ = await prepare_mesh_task(
        client,
        headers,
        db_session,
        [
            ("first.png", b"hunyuan-mixed-image", "image/png"),
            ("input.mp4", b"hunyuan-mixed-video", "video/mp4"),
        ],
    )
    rejected = await client.post(
        f"/api/v1/reconstruction/mesh/start/{task_id}",
        headers=headers,
        json={"algorithm": "hunyuan3d", "input_file_ids": input_ids, "params": {}},
    )
    assert rejected.status_code == 400
    removed = await client.post(
        f"/api/v1/reconstruction/hunyuan3d/start/{task_id}",
        headers=headers,
        json={"input_file_ids": input_ids},
    )
    assert removed.status_code == 404


@pytest.mark.asyncio
async def test_hunyuan3d_rejects_empty_input_and_derived_thumbnail(client, db_session):
    headers = await register(client, "hunyuanderivative")
    task_id, source_ids, _ = await prepare_mesh_task(
        client,
        headers,
        db_session,
        [("source.png", b"hunyuan-source", "image/png")],
    )
    empty = await client.post(
        f"/api/v1/reconstruction/mesh/start/{task_id}",
        headers=headers,
        json={"algorithm": "hunyuan3d", "input_file_ids": []},
    )
    assert empty.status_code == 422

    source_id = source_ids[0]
    thumbnail_id = await upload(client, headers, "thumbnail.jpg", b"hunyuan-thumbnail", "image/jpeg")
    records = list((await db_session.execute(
        select(FileRecord).where(FileRecord.public_id.in_([source_id, thumbnail_id]))
    )).scalars())
    source = next(record for record in records if record.public_id == source_id)
    thumbnail = next(record for record in records if record.public_id == thumbnail_id)
    thumbnail.category = FileCategory.PREVIEW_IMAGE
    db_session.add(FileDerivativeRecord(
        source_file=source,
        derivative_file=thumbnail,
        variant=FileDerivativeVariant.THUMBNAIL,
    ))
    await db_session.commit()

    rejected = await client.post(
        f"/api/v1/reconstruction/mesh/start/{task_id}",
        headers=headers,
        json={"algorithm": "hunyuan3d", "input_file_ids": [thumbnail_id]},
    )
    assert rejected.status_code == 400


def test_hunyuan3d_command_uses_server_conda_and_glb_output_path(tmp_path):
    spec = _algorithm_specs()["hunyuan3d"]
    input_path = tmp_path / "input.png"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    command = _build_command(spec, "recon_test", tmp_path / "images", input_path, output_dir)
    assert command == [
        "/data1/lzh/anaconda3/bin/conda",
        "run",
        "-n",
        "hunyuan3d",
        "python",
        "example.py",
        str(input_path),
        "-o",
        str(output_dir / "hunyuan3d_result.glb"),
    ]

@pytest.mark.asyncio
async def test_hunyuan3d_registers_primary_glb_and_all_outputs(client, db_session, tmp_path):
    headers = await register(client, "hunyuanresults")
    task_id = await create_task(client, headers)
    task = await TaskService.get_by_public_id(db_session, task_id)
    output_dir = tmp_path / "output"
    asset_dir = output_dir / "asset"
    texture_dir = asset_dir / "textures"
    texture_dir.mkdir(parents=True)
    (output_dir / "hunyuan3d_result.glb").write_bytes(b"glb-result")
    (asset_dir / "mesh.obj").write_text("mtllib mesh.mtl", encoding="utf-8")
    (asset_dir / "mesh.mtl").write_text("map_Kd textures/albedo.png", encoding="utf-8")
    (texture_dir / "albedo.png").write_bytes(b"texture")
    (asset_dir / "metadata.json").write_text("{}", encoding="utf-8")
    (asset_dir / "ignored.tmp").write_bytes(b"temporary")

    error = await _register_hunyuan3d_results(db_session, task, output_dir, tmp_path)
    assert error is None
    task.status = TaskStatus.COMPLETED
    task.current_stage = "completed"
    await db_session.commit()

    status = await client.get(f"/api/v1/reconstruction/status/{task_id}", headers=headers)
    assert status.status_code == 200
    data = status.json()
    assert len(data["result_files"]) == 5
    records = list((await db_session.execute(
        select(FileRecord).where(FileRecord.public_id.in_(
            [item["file_id"] for item in data["result_files"]]
        ))
    )).scalars())
    primary = next(record for record in records if (record.metainfo or {}).get("primary_result"))
    assert data["result_id"] == primary.public_id
    assert primary.filename == "hunyuan3d_result.glb"
    assert primary.category == FileCategory.GLB_MODEL
    assert all(item["file_type"] for item in data["result_files"])
    result_by_filename = {item["filename"]: item for item in data["result_files"]}
    assert result_by_filename["mesh.obj"]["file_type"] == "model"
    assert result_by_filename["albedo.png"]["file_type"] == "image"
    assert {record.mime_type for record in records} == {
        "model/gltf-binary",
        "model/obj",
        "model/mtl",
        "image/png",
        "application/json",
    }
    assert all((record.metainfo or {}).get("generated_by") == "hunyuan3d" for record in records)


@pytest.mark.asyncio
async def test_same_task_keeps_dash_and_hunyuan_mesh_results(client, db_session, tmp_path):
    headers = await register(client, "multimeshresults")
    task_id = await create_task(client, headers)
    task = await TaskService.get_by_public_id(db_session, task_id)

    dash_output = tmp_path / "dash-output"
    dash_mesh_dir = dash_output / "dash_gaussian_mesh"
    dash_mesh_dir.mkdir(parents=True)
    (dash_mesh_dir / "dash_gaussian_mesh.obj").write_text("dash-obj", encoding="utf-8")
    assert await _register_dash_gaussian_mesh_results(db_session, task, dash_output) is None

    hunyuan_output = tmp_path / "hunyuan-output"
    hunyuan_output.mkdir()
    (hunyuan_output / "hunyuan3d_result.glb").write_bytes(b"glb")
    (hunyuan_output / "mesh.obj").write_text("obj", encoding="utf-8")
    assert await _register_hunyuan3d_results(db_session, task, hunyuan_output, tmp_path) is None
    task.algorithm = "hunyuan3d"
    task.mesh_algorithm = "hunyuan3d"
    task.status = TaskStatus.COMPLETED
    task.current_stage = "mesh_completed"
    await db_session.commit()

    status = await client.get(f"/api/v1/reconstruction/status/{task_id}", headers=headers)
    categories = {item["category"] for item in status.json()["result_files"]}
    assert categories == {
        FileCategory.MESH_MODEL.value,
        FileCategory.GLB_MODEL.value,
    }


@pytest.mark.asyncio
async def test_hunyuan3d_result_collection_requires_primary_glb_not_obj(client, db_session, tmp_path):
    headers = await register(client, "hunyuanmissingglb")
    task_id = await create_task(client, headers)
    task = await TaskService.get_by_public_id(db_session, task_id)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "mesh.glb").write_bytes(b"glb-result")
    error = await _register_hunyuan3d_results(db_session, task, output_dir, tmp_path)
    assert error == f"Hunyuan3D output missing: {output_dir / 'hunyuan3d_result.glb'}"

    output_dir = tmp_path / "glb-only-output"
    output_dir.mkdir()
    (output_dir / "hunyuan3d_result.glb").write_bytes(b"glb-result")
    error = await _register_hunyuan3d_results(db_session, task, output_dir, tmp_path)
    assert error is None
