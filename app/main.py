import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.database import init_db
from app.api.v1 import router as v1_router

logger = logging.getLogger("app.validation")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from app.core.storage import get_storage_backend
    from app.api.v1.reconstruction import recover_stale_reconstruction_tasks

    await get_storage_backend().ensure_bucket()
    await recover_stale_reconstruction_tasks()
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v1_router, prefix=settings.api_prefix)


@app.exception_handler(StarletteHTTPException)
async def log_http_error(request: Request, exc: StarletteHTTPException):
    client = request.client.host if request.client else "unknown"
    logger.warning(
        "HTTP request failed: client=%s method=%s path=%s status=%s detail=%s",
        client,
        request.method,
        request.url.path,
        exc.status_code,
        exc.detail,
    )
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def log_request_validation_error(request: Request, exc: RequestValidationError):
    safe_errors = [
        {
            "loc": ".".join(str(part) for part in error.get("loc", [])),
            "type": error.get("type", ""),
            "msg": error.get("msg", ""),
        }
        for error in exc.errors()
    ]
    client = request.client.host if request.client else "unknown"
    logger.warning(
        "Request validation failed: client=%s method=%s path=%s errors=%s",
        client,
        request.method,
        request.url.path,
        safe_errors,
    )
    return await request_validation_exception_handler(request, exc)


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_name}
