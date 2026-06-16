import pytest

from app.core.config import settings
from app.services.gpu_scheduler import GPUScheduler


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.zsets = {}
        self.closed = 0

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, _key_count, key, *args):
        if "zremrangebyscore" in script:
            now = float(args[0])
            ttl = float(args[1])
            limit = int(args[2])
            member = args[3]
            zset = self.zsets.setdefault(key, {})
            for existing, score in list(zset.items()):
                if score <= now:
                    zset.pop(existing, None)
            if limit <= 0 or len(zset) >= limit:
                return 0
            zset[member] = now + ttl
            return 1
        if "zscore" in script:
            member = args[0]
            ttl = float(args[1])
            now = float(args[2])
            zset = self.zsets.setdefault(key, {})
            if member in zset:
                zset[member] = now + ttl
                return 1
            return 0
        if "zrange" in script:
            task_id = args[0]
            zset = self.zsets.setdefault(key, {})
            removed = 0
            for member in list(zset):
                if task_id in member:
                    zset.pop(member, None)
                    removed += 1
            return removed
        value = args[0]
        if "expire" in script:
            return int(self.values.get(key) == value)
        if "string.find" in script:
            if value in self.values.get(key, ""):
                self.values.pop(key, None)
                return 1
            return 0
        if self.values.get(key) == value:
            self.values.pop(key, None)
            return 1
        return 0

    async def zrem(self, key, member):
        zset = self.zsets.setdefault(key, {})
        if member in zset:
            zset.pop(member, None)
            return 1
        return 0

    async def aclose(self):
        self.closed += 1


@pytest.mark.asyncio
async def test_local_scheduler_leases_each_gpu_once(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr("app.services.gpu_scheduler.get_redis_client", lambda: redis)
    monkeypatch.setattr(GPUScheduler, "_memory_used_mb", staticmethod(lambda: {"0": 0, "1": 10}))
    monkeypatch.setattr(settings, "gpu_scheduler_mode", "local")
    monkeypatch.setattr(settings, "gpu_device_ids", "0,1")
    monkeypatch.setattr(settings, "worker_node_id", "node-a")
    monkeypatch.setattr(settings, "worker_executor_id", "worker-a")

    first = (await GPUScheduler.acquire("recon_first", user_id=1, concurrency_quota=10)).lease
    second = (await GPUScheduler.acquire("recon_second", user_id=1, concurrency_quota=10)).lease
    third = await GPUScheduler.acquire("recon_third", user_id=1, concurrency_quota=10)

    assert first and first.device_id == "0"
    assert second and second.device_id == "1"
    assert third.lease is None
    assert third.queue_reason == "gpu_capacity"
    assert await GPUScheduler.renew(first)
    assert await GPUScheduler.release(first)
    assert (await GPUScheduler.acquire("recon_fourth", user_id=1, concurrency_quota=10)).lease.device_id == "0"


@pytest.mark.asyncio
async def test_busy_gpus_keep_task_waiting(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr("app.services.gpu_scheduler.get_redis_client", lambda: redis)
    monkeypatch.setattr(GPUScheduler, "_memory_used_mb", staticmethod(lambda: {"0": 900, "1": 800}))
    monkeypatch.setattr(settings, "gpu_scheduler_mode", "local")
    monkeypatch.setattr(settings, "gpu_device_ids", "0,1")
    monkeypatch.setattr(settings, "gpu_memory_busy_threshold_mb", 512)

    result = await GPUScheduler.acquire("recon_wait", user_id=1, concurrency_quota=10)
    assert result.lease is None
    assert result.queue_reason == "gpu_capacity"


@pytest.mark.asyncio
async def test_k8s_scheduler_uses_executor_scope_and_logical_device_zero(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr("app.services.gpu_scheduler.get_redis_client", lambda: redis)
    monkeypatch.setattr(GPUScheduler, "_memory_used_mb", staticmethod(lambda: {"7": 32}))
    monkeypatch.setattr(settings, "gpu_scheduler_mode", "k8s")
    monkeypatch.setattr(settings, "gpu_device_ids", "0")
    monkeypatch.setattr(settings, "worker_node_id", "node-b")
    monkeypatch.setattr(settings, "worker_executor_id", "pod-uid")

    lease = (await GPUScheduler.acquire("recon_k8s", user_id=1, concurrency_quota=10)).lease

    assert lease
    assert lease.node_id == "node-b"
    assert lease.executor_id == "pod-uid"
    assert lease.lease_scope == "pod-uid"
    assert lease.device_id == "0"
    assert "gpu-lease:pod-uid:0" in redis.values


@pytest.mark.asyncio
async def test_user_concurrency_quota_blocks_second_task(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr("app.services.gpu_scheduler.get_redis_client", lambda: redis)
    monkeypatch.setattr(GPUScheduler, "_memory_used_mb", staticmethod(lambda: {"0": 0, "1": 0}))
    monkeypatch.setattr(settings, "gpu_scheduler_mode", "local")
    monkeypatch.setattr(settings, "gpu_device_ids", "0,1")

    first = await GPUScheduler.acquire("recon_first", user_id=7, concurrency_quota=1)
    second = await GPUScheduler.acquire("recon_second", user_id=7, concurrency_quota=1)

    assert first.lease is not None
    assert second.lease is None
    assert second.queue_reason == "user_gpu_concurrency"
    assert await GPUScheduler.release(first.lease)
    assert (await GPUScheduler.acquire("recon_third", user_id=7, concurrency_quota=1)).lease is not None
