import pytest


@pytest.fixture
async def auth_headers(client):
    await client.post("/api/v1/auth/register", json={
        "username": "fileuser",
        "email": "file@example.com",
        "password": "testpass123",
    })
    resp = await client.post("/api/v1/auth/login", json={
        "username": "fileuser",
        "password": "testpass123",
    })
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_files_empty(client, auth_headers):
    resp = await client.get("/api/v1/files", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_get_file_not_found(client, auth_headers):
    resp = await client.get("/api/v1/files/99999", headers=auth_headers)
    assert resp.status_code == 404
