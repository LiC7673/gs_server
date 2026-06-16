from fastapi import APIRouter

from . import auth, files, reconstruction, upload, users

router = APIRouter()
router.include_router(auth.router)
router.include_router(users.router)
router.include_router(files.router)
router.include_router(upload.router)
router.include_router(reconstruction.router)
