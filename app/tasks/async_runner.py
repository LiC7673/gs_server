import asyncio
from typing import Awaitable, TypeVar

from app.core.database import engine


Result = TypeVar("Result")


async def _run_and_dispose(awaitable: Awaitable[Result]) -> Result:
    try:
        return await awaitable
    finally:
        # Celery tasks are synchronous entrypoints and asyncio.run() creates a
        # fresh loop each time. Do not keep asyncpg connections from an old loop.
        await engine.dispose()


def run_async(awaitable: Awaitable[Result]) -> Result:
    return asyncio.run(_run_and_dispose(awaitable))
