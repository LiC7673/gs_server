import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.main import app
from app.core.database import Base, get_db
from app.core.exceptions import NotFoundException

TEST_DB_URL = "sqlite+aiosqlite://"


class MemoryStorage:
    def __init__(self):
        self.objects = {}

    async def ensure_bucket(self):
        return None

    async def bucket_exists(self):
        return True

    async def save(self, object_key, content):
        self.objects[object_key] = bytes(content)
        return object_key

    async def save_fileobj(self, object_key, fileobj):
        self.objects[object_key] = fileobj.read()
        return object_key

    async def upload_file(self, object_key, local_path):
        from pathlib import Path

        self.objects[object_key] = Path(local_path).read_bytes()
        return object_key

    async def download_file(self, object_key, local_path):
        from pathlib import Path

        path = Path(local_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.objects[object_key])
        return path

    async def read(self, object_key):
        return self.objects[object_key]

    async def read_range(self, object_key, start, end):
        return self.objects[object_key][start:end + 1]

    async def delete(self, object_key):
        self.objects.pop(object_key, None)
        return True

    async def exists(self, object_key):
        return object_key in self.objects


@pytest.fixture(autouse=True)
def fake_infrastructure(monkeypatch):
    import app.api.v1.files as files_api
    import app.api.v1.reconstruction as reconstruction_api
    import app.services.file_service as file_service
    import app.services.media_service as media_service
    import app.services.upload_service as upload_service
    from app.core.celery_app import celery_app
    from app.core.config import settings

    storage = MemoryStorage()
    sessions = {}
    queued_tasks = []

    class QueuedTask:
        def __init__(self, task_id):
            self.id = task_id

    def send_task(name, args=None, queue=None, **kwargs):
        task_id = f"test-task-{len(queued_tasks) + 1}"
        queued_tasks.append({"id": task_id, "name": name, "args": args or [], "queue": queue})
        return QueuedTask(task_id)

    async def write_json(key, value, ttl_seconds):
        sessions[key] = value.copy()

    async def read_json(key):
        if key not in sessions:
            raise NotFoundException("Download session not found")
        return sessions[key].copy()

    monkeypatch.setattr(file_service, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(upload_service, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(media_service, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(reconstruction_api, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(file_service, "write_json", write_json)
    monkeypatch.setattr(file_service, "read_json", read_json)
    monkeypatch.setattr(settings, "mock_auth_enabled", False)
    monkeypatch.setattr(celery_app, "send_task", send_task)
    storage.queued_tasks = queued_tasks
    return storage


@pytest.fixture(scope="session")
def event_loop():
    import asyncio
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def engine():
    eng = create_async_engine(TEST_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db_session(engine) -> AsyncSession:
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
async def client(db_session: AsyncSession):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
