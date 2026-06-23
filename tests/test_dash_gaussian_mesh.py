import pytest

from app.api.v1.reconstruction import (
    _algorithm_specs,
    _build_dash_gaussian_mesh_pipeline,
    _normalize_dash_gaussian_mesh_params,
    _register_dash_gaussian_mesh_results,
)
from app.models.file import FileCategory
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


async def upload(client, headers, name, content, mime_type="model/ply"):
    file_hash = compute_hash(content)
    init = await client.post("/api/v1/upload/init", headers=headers, json={
        "filename": name,
        "file_size": len(content),
        "chunk_size": len(content),
        "mime_type": mime_type,
        "file_hash": file_hash,
    })
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
    return await client.post("/api/v1/reconstruction/tasks", headers=headers, json={
        "title": "dash gaussian mesh test",
        "algorithm": algorithm,
        "params": {},
    })


@pytest.mark.asyncio
async def test_dash_gaussian_mesh_algorithm_and_params(client):
    headers = await register(client, "dashmeshparams")
    algorithms = await client.get("/api/v1/reconstruction/algorithms", headers=headers)
    assert algorithms.status_code == 200
    names = {item["name"] for item in algorithms.json()["algorithms"]}
    assert "dash_gaussian_mesh" not in names
    mesh_algorithms = await client.get("/api/v1/reconstruction/mesh/algorithms", headers=headers)
    assert mesh_algorithms.status_code == 200
    mesh_names = {item["name"] for item in mesh_algorithms.json()["algorithms"]}
    assert mesh_names == {"dash_gaussian_mesh", "hunyuan3d"}
    dash_mesh = next(
        item for item in mesh_algorithms.json()["algorithms"] if item["name"] == "dash_gaussian_mesh"
    )
    assert dash_mesh["dependencies"] == {
        "required_stage": "gaussian_completed",
        "required_gaussian_algorithms": ["dash_gaussian"],
        "required_input_type": "ply_model",
        "description": "只能在 dash_gaussian 高斯阶段成功后运行，输入必须是该任务的 PLY 结果。",
    }
    mesh_params = {item["param_name"]: item for item in dash_mesh["params"]}
    assert mesh_params["radius"]["display_name"] == "半径过滤"
    assert mesh_params["radius"]["default_value"] == 4
    assert mesh_params["voxel_size"]["default_value"] == 0.02

    created = await create_task(client, headers)
    assert created.status_code == 200
    assert "mesh_params" not in created.json()
    rejected = await client.post("/api/v1/reconstruction/tasks", headers=headers, json={
        "title": "old create body",
        "algorithm": "dash_gaussian",
        "params": {},
        "mesh_params": {"radius": 10},
    })
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_dash_gaussian_mesh_requires_dash_gaussian_predecessor(client, db_session):
    headers = await register(client, "dashmeshdependency")
    created = await create_task(client, headers, algorithm="anysplat")
    task_id = created.json()["task_id"]
    ply_id = await upload(client, headers, "anysplat-result.ply", b"ply", "model/ply")
    cfg_id = await upload(client, headers, "cfg_args", b"Namespace(model_path='/old')", "text/plain")
    task = await TaskService.get_by_public_id(db_session, task_id)
    ply = await FileService.get_by_identifier_for_user(db_session, ply_id, task.user_id)
    cfg = await FileService.get_by_identifier_for_user(db_session, cfg_id, task.user_id)
    ply.metainfo = {
        **(ply.metainfo or {}),
        "generated_by": "anysplat",
        "generation_id": "anysplat_dependency",
        "relative_path": "point_cloud/iteration_30000/point_cloud.ply",
        "primary_result": True,
    }
    cfg.metainfo = {
        **(cfg.metainfo or {}),
        "generated_by": "anysplat",
        "generation_id": "anysplat_dependency",
        "relative_path": "cfg_args",
        "primary_result": False,
    }
    await TaskService.add_file_link(db_session, task, ply, TaskFileRole.RESULT)
    await TaskService.add_file_link(db_session, task, cfg, TaskFileRole.RESULT)
    task.status = TaskStatus.COMPLETED
    task.current_stage = "gaussian_completed"
    await db_session.commit()

    rejected = await client.post(
        f"/api/v1/reconstruction/mesh/start/{task_id}",
        headers=headers,
        json={"algorithm": "dash_gaussian_mesh", "input_file_ids": [ply_id]},
    )

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "dash_gaussian_mesh requires a completed dash_gaussian Gaussian result"


@pytest.mark.asyncio
async def test_start_request_rejects_removed_algorithm_override(client):
    headers = await register(client, "dashmeshinputs")
    ply_id = await upload(client, headers, "gaussians.ply", b"ply-input", "model/ply")

    task_id = (await create_task(client, headers)).json()["task_id"]
    started = await client.post(
        f"/api/v1/reconstruction/start/{task_id}",
        headers=headers,
        json={
            "algorithm": "dash_gaussian_mesh",
            "input_type": "ply",
            "input_file_ids": [ply_id],
        },
    )
    assert started.status_code == 422


@pytest.mark.asyncio
async def test_mesh_retry_uses_same_task_latest_result_file(client, db_session):
    headers = await register(client, "dashmeshfollowup")
    ply_id = await upload(client, headers, "stage_result.ply", b"ply-result", "model/ply")
    cfg_id = await upload(client, headers, "cfg_args", b"Namespace(model_path='/old', source_path='/old')", "text/plain")
    created = await client.post("/api/v1/reconstruction/tasks", headers=headers, json={
        "title": "same task followup",
        "algorithm": "dash_gaussian",
        "params": {"iterations": 30000},
    })
    assert created.status_code == 200
    task_id = created.json()["task_id"]
    task = await TaskService.get_by_public_id(db_session, task_id)
    record = await FileService.get_by_identifier_for_user(db_session, ply_id, task.user_id)
    record.metainfo = {
        **(record.metainfo or {}),
        "generated_by": "dash_gaussian",
        "generation_id": "dash_gaussian_followup",
        "relative_path": "point_cloud/iteration_30000/point_cloud.ply",
        "primary_result": True,
    }
    cfg_record = await FileService.get_by_identifier_for_user(db_session, cfg_id, task.user_id)
    cfg_record.metainfo = {
        **(cfg_record.metainfo or {}),
        "generated_by": "dash_gaussian",
        "generation_id": "dash_gaussian_followup",
        "relative_path": "cfg_args",
        "primary_result": False,
    }
    await TaskService.add_file_link(db_session, task, record, TaskFileRole.RESULT)
    await TaskService.add_file_link(db_session, task, cfg_record, TaskFileRole.RESULT)
    task.status = TaskStatus.COMPLETED
    task.current_stage = "gaussian_completed"
    task.progress = 100.0
    await db_session.commit()

    started = await client.post(
        f"/api/v1/reconstruction/mesh/start/{task_id}",
        headers=headers,
        json={
            "algorithm": "dash_gaussian_mesh",
            "input_file_ids": [ply_id],
            "params": {"radius": 10},
        },
    )
    assert started.status_code == 200
    assert started.json()["algorithm"] == "dash_gaussian_mesh"
    assert started.json()["input_type"] == "ply_model"

    status = await client.get(f"/api/v1/reconstruction/status/{task_id}", headers=headers)
    assert status.status_code == 200
    body = status.json()
    assert body["algorithm"] == "dash_gaussian_mesh"
    assert body["params"]["radius"] == 10
    assert body["input_file_ids"] == [ply_id]
    assert body["result_files"][0]["category"] == FileCategory.PLY_MODEL.value


@pytest.mark.asyncio
async def test_mesh_start_validates_params_and_task_ply(client, db_session):
    headers = await register(client, "dashmeshvalidation")
    ply_id = await upload(client, headers, "valid_result.ply", b"valid-ply", "model/ply")
    cfg_id = await upload(client, headers, "cfg_args", b"Namespace(model_path='/old', source_path='/old')", "text/plain")
    unrelated_id = await upload(client, headers, "unrelated.ply", b"unrelated-ply", "model/ply")
    task_id = (await create_task(client, headers)).json()["task_id"]
    task = await TaskService.get_by_public_id(db_session, task_id)
    record = await FileService.get_by_identifier_for_user(db_session, ply_id, task.user_id)
    record.metainfo = {
        **(record.metainfo or {}),
        "generated_by": "dash_gaussian",
        "generation_id": "dash_gaussian_validation",
        "relative_path": "point_cloud/iteration_30000/point_cloud.ply",
        "primary_result": True,
    }
    cfg_record = await FileService.get_by_identifier_for_user(db_session, cfg_id, task.user_id)
    cfg_record.metainfo = {
        **(cfg_record.metainfo or {}),
        "generated_by": "dash_gaussian",
        "generation_id": "dash_gaussian_validation",
        "relative_path": "cfg_args",
        "primary_result": False,
    }
    await TaskService.add_file_link(db_session, task, record, TaskFileRole.RESULT)
    await TaskService.add_file_link(db_session, task, cfg_record, TaskFileRole.RESULT)
    task.status = TaskStatus.COMPLETED
    task.current_stage = "gaussian_completed"
    await db_session.commit()

    unrelated = await client.post(
        f"/api/v1/reconstruction/mesh/start/{task_id}",
        headers=headers,
        json={"algorithm": "dash_gaussian_mesh", "input_file_ids": [unrelated_id]},
    )
    assert unrelated.status_code == 400
    invalid_radius = await client.post(
        f"/api/v1/reconstruction/mesh/start/{task_id}",
        headers=headers,
        json={"algorithm": "dash_gaussian_mesh", "input_file_ids": [ply_id], "params": {"radius": 3}},
    )
    assert invalid_radius.status_code == 422
    invalid_unknown = await client.post(
        f"/api/v1/reconstruction/mesh/start/{task_id}",
        headers=headers,
        json={"algorithm": "dash_gaussian_mesh", "input_file_ids": [ply_id], "params": {"unknown": True}},
    )
    assert invalid_unknown.status_code == 422


def test_dash_gaussian_mesh_pipeline_commands(tmp_path):
    params = _normalize_dash_gaussian_mesh_params({"iteration": 1234, "radius": 10})
    spec = _algorithm_specs()["dash_gaussian_mesh"]
    input_path = tmp_path / "input.ply"
    scratch_dir = tmp_path / "scratch"
    output_dir = tmp_path / "output"
    pipeline = _build_dash_gaussian_mesh_pipeline(spec, input_path, scratch_dir, output_dir, params)

    assert pipeline.model_root == output_dir / "dash_gaussian_mesh_model"
    assert pipeline.point_cloud_path == output_dir / "dash_gaussian_mesh_model" / "point_cloud" / "iteration_1234" / "point_cloud.ply"
    assert pipeline.mesh_output_dir == output_dir / "dash_gaussian_mesh"
    assert pipeline.mesh_path == output_dir / "dash_gaussian_mesh" / "dash_gaussian_mesh.obj"
    assert pipeline.commands[0] == [
        "/data1/lzh/anaconda3/bin/conda",
        "run",
        "-n",
        "DashGaussian",
        "python",
        "scripts/filter_gaussians_by_radius.py",
        "-i",
        str(input_path),
        "-o",
        str(scratch_dir / "mesh_pipeline" / "radius_filtered.ply"),
        "-r",
        "10",
    ]
    assert "--keep" in pipeline.commands[1]
    assert pipeline.commands[2][pipeline.commands[2].index("-m") + 1] == str(pipeline.model_root)
    assert pipeline.commands[2][pipeline.commands[2].index("--output") + 1] == str(pipeline.mesh_path)

    default_pipeline = _build_dash_gaussian_mesh_pipeline(
        spec,
        input_path,
        scratch_dir,
        output_dir,
        _normalize_dash_gaussian_mesh_params({}),
    )
    assert default_pipeline.commands[0][default_pipeline.commands[0].index("-r") + 1] == "4"


@pytest.mark.asyncio
async def test_dash_gaussian_mesh_registers_obj_result(client, db_session, tmp_path):
    headers = await register(client, "dashmeshresult")
    created = await create_task(client, headers)
    task_id = created.json()["task_id"]
    task = await TaskService.get_by_public_id(db_session, task_id)
    output_dir = tmp_path / "output"
    mesh_dir = output_dir / "dash_gaussian_mesh"
    mesh_dir.mkdir(parents=True)
    (mesh_dir / "dash_gaussian_mesh.obj").write_text("obj-result", encoding="utf-8")
    (mesh_dir / "dash_gaussian_mesh.mtl").write_text("newmtl material", encoding="utf-8")

    error = await _register_dash_gaussian_mesh_results(db_session, task, output_dir)
    assert error is None
    task.status = TaskStatus.COMPLETED
    task.current_stage = "completed"
    await db_session.commit()

    status = await client.get(f"/api/v1/reconstruction/status/{task_id}", headers=headers)
    assert status.status_code == 200
    data = status.json()
    assert data["result_id"]
    assert data["result_id"] == next(
        item["file_id"] for item in data["result_files"] if item["filename"] == "dash_gaussian_mesh.obj"
    )
    categories = {item["filename"]: item["category"] for item in data["result_files"]}
    assert categories["dash_gaussian_mesh.obj"] == FileCategory.MESH_MODEL.value
    assert categories["dash_gaussian_mesh.mtl"] == FileCategory.OTHER.value
    detail = await client.get(f"/api/v1/reconstruction/tasks/{task_id}", headers=headers)
    assert detail.status_code == 200
    results_by_filename = {item["filename"]: item for item in detail.json()["results"]}
    assert results_by_filename["dash_gaussian_mesh.obj"] == {
        "file_id": data["result_id"],
        "filename": "dash_gaussian_mesh.obj",
        "file_type": "model",
        "category": "mesh_model",
        "mime_type": "model/obj",
        "size_bytes": len("obj-result".encode("utf-8")),
    }
    assert results_by_filename["dash_gaussian_mesh.mtl"]["category"] == "mesh_model"
    assert results_by_filename["dash_gaussian_mesh.mtl"]["size_bytes"] == len("newmtl material".encode("utf-8"))


@pytest.mark.asyncio
async def test_dash_gaussian_mesh_result_collection_reports_missing_obj(client, db_session, tmp_path):
    headers = await register(client, "dashmeshmissing")
    created = await create_task(client, headers)
    task = await TaskService.get_by_public_id(db_session, created.json()["task_id"])
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    error = await _register_dash_gaussian_mesh_results(db_session, task, output_dir)
    assert "dash_gaussian_mesh.obj" in error
