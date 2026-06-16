import asyncio
from pathlib import Path
from typing import BinaryIO, Union

from botocore.exceptions import ClientError

from app.core.config import settings


class S3StorageBackend:
    def __init__(self):
        import boto3

        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )
        self.bucket = settings.s3_bucket

    async def ensure_bucket(self) -> None:
        def _ensure() -> None:
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except ClientError:
                self.client.create_bucket(Bucket=self.bucket)

        await asyncio.to_thread(_ensure)

    async def bucket_exists(self) -> bool:
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
            return True
        except ClientError:
            return False

    async def save(self, object_key: str, content: bytes) -> str:
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=object_key,
            Body=content,
        )
        return object_key

    async def save_fileobj(self, object_key: str, fileobj: BinaryIO) -> str:
        await asyncio.to_thread(self.client.upload_fileobj, fileobj, self.bucket, object_key)
        return object_key

    async def upload_file(self, object_key: str, local_path: Union[str, Path]) -> str:
        await asyncio.to_thread(self.client.upload_file, str(local_path), self.bucket, object_key)
        return object_key

    async def download_file(self, object_key: str, local_path: Union[str, Path]) -> Path:
        path = Path(local_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.client.download_file, self.bucket, object_key, str(path))
        return path

    async def read(self, object_key: str) -> bytes:
        def _read() -> bytes:
            response = self.client.get_object(Bucket=self.bucket, Key=object_key)
            return response["Body"].read()

        return await asyncio.to_thread(_read)

    async def read_range(self, object_key: str, start: int, end: int) -> bytes:
        def _read_range() -> bytes:
            response = self.client.get_object(
                Bucket=self.bucket,
                Key=object_key,
                Range=f"bytes={start}-{end}",
            )
            return response["Body"].read()

        return await asyncio.to_thread(_read_range)

    async def delete(self, object_key: str) -> bool:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=object_key)
        return True

    async def exists(self, object_key: str) -> bool:
        try:
            await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=object_key)
            return True
        except ClientError:
            return False


def get_storage_backend() -> S3StorageBackend:
    return S3StorageBackend()
