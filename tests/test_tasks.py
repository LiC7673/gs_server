import pytest

from app.api.v1.reconstruction import _task_response
from app.models.task import TaskRecord, TaskStatus, TaskVisibility


@pytest.fixture
async def auth_headers(client):
    await client.post("/api/v1/auth/register", json={
        "username": "taskuser",
        "email": "task@example.com",
        "password": "testpass123",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "username": "taskuser",
        "password": "testpass123",
    })
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_legacy_tasks_route_removed(client, auth_headers):
    resp = await client.get("/api/v1/tasks", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_reconstruction_tasks_empty(client, auth_headers):
    resp = await client.get("/api/v1/reconstruction/tasks", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_create_reconstruction_task_has_string_id(client, auth_headers):
    resp = await client.post(
        "/api/v1/reconstruction/tasks",
        headers=auth_headers,
        json={"title": "demo", "algorithm": "anysplat", "params": {}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"].startswith("recon_")
    assert "task_type" not in data
    assert "parent_id" not in data
    assert data["visibility"] == "private"


@pytest.mark.asyncio
async def test_task_parent_and_children_api_removed(client, auth_headers):
    created = await client.post(
        "/api/v1/reconstruction/tasks",
        headers=auth_headers,
        json={"title": "single container", "algorithm": "anysplat", "params": {}},
    )
    assert created.status_code == 200
    task_id = created.json()["task_id"]

    detail = await client.get(f"/api/v1/reconstruction/tasks/{task_id}", headers=auth_headers)
    assert detail.status_code == 200
    assert "task_type" not in detail.json()
    assert "parent_id" not in detail.json()

    children = await client.get(f"/api/v1/reconstruction/tasks/{task_id}/children", headers=auth_headers)
    assert children.status_code == 404


def test_task_response_tolerates_nullish_legacy_fields():
    task = TaskRecord(
        user_id=1,
        algorithm="anysplat",
        status=TaskStatus.COMPLETED,
        visibility=TaskVisibility.PUBLIC,
    )
    task.public_id = "recon_nullish"
    task.title = None
    task.params = None
    task.current_stage = None
    task.progress = None
    task.input_kind = None
    task.cancel_requested = None
    task.created_at = None
    task.file_links = []

    response = _task_response(task)

    assert response.title == ""
    assert response.params == {}
    assert response.current_stage == "completed"
    assert response.progress == 0.0
    assert response.input_kind == ""
    assert response.cancel_requested is False
    assert response.created_at == ""
