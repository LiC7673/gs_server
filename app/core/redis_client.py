import json
from typing import Any, Dict

from redis.asyncio import Redis

from app.core.config import settings
from app.core.exceptions import NotFoundException


def get_redis_client() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


async def write_json(key: str, value: Dict[str, Any], ttl_seconds: int) -> None:
    client = get_redis_client()
    try:
        await client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_seconds)
    finally:
        await client.aclose()


async def read_json(key: str) -> Dict[str, Any]:
    client = get_redis_client()
    try:
        payload = await client.get(key)
    finally:
        await client.aclose()
    if not payload:
        raise NotFoundException("Download session not found")
    return json.loads(payload)
