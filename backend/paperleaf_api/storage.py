"""PDF 对象存储边界及 Range 解析。"""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ObjectStorage(Protocol):
    async def put(self, key: str, content: bytes, content_type: str) -> None: ...
    async def read(self, key: str, start: int = 0, end: int | None = None) -> bytes: ...
    async def size(self, key: str) -> int: ...
    async def delete(self, key: str) -> None: ...
    async def check_ready(self) -> dict[str, str]: ...


class LocalObjectStorage:
    """本地开发存储；所有路径都被约束在指定根目录。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError("非法对象路径")
        return path

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    async def read(self, key: str, start: int = 0, end: int | None = None) -> bytes:
        path = self._path(key)
        with path.open("rb") as stream:
            stream.seek(start)
            return stream.read(None if end is None else end - start + 1)

    async def size(self, key: str) -> int:
        return self._path(key).stat().st_size

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    async def check_ready(self) -> dict[str, str]:
        return {
            "status": "ready" if self.root.is_dir() else "degraded",
            "backend": "local",
            "bucket": "not_applicable",
        }


class MinioObjectStorage:
    """MinIO/S3-compatible 生产适配器。"""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ) -> None:
        from minio import Minio

        self.client = Minio(
            endpoint, access_key=access_key, secret_key=secret_key, secure=secure
        )
        self.bucket = bucket

    async def ensure_bucket(self) -> None:
        exists = await asyncio.to_thread(self.client.bucket_exists, self.bucket)
        if not exists:
            await asyncio.to_thread(self.client.make_bucket, self.bucket)

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        await self.ensure_bucket()
        await asyncio.to_thread(
            self.client.put_object,
            self.bucket,
            key,
            io.BytesIO(content),
            len(content),
            content_type=content_type,
        )

    async def read(self, key: str, start: int = 0, end: int | None = None) -> bytes:
        length = None if end is None else end - start + 1

        def _read() -> bytes:
            response = self.client.get_object(self.bucket, key, offset=start, length=length)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_read)

    async def size(self, key: str) -> int:
        stat = await asyncio.to_thread(self.client.stat_object, self.bucket, key)
        return int(stat.size)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.remove_object, self.bucket, key)

    async def check_ready(self) -> dict[str, str]:
        try:
            exists = await asyncio.to_thread(self.client.bucket_exists, self.bucket)
        except Exception:
            return {"status": "degraded", "backend": "minio", "bucket": "unreachable"}
        return {
            "status": "ready" if exists else "degraded",
            "backend": "minio",
            "bucket": "available" if exists else "missing",
        }


def create_storage(config: object) -> ObjectStorage:
    if getattr(config, "storage_backend", "local") == "minio":
        return MinioObjectStorage(
            endpoint=config.minio_endpoint,
            access_key=config.minio_access_key,
            secret_key=config.minio_secret_key,
            bucket=config.minio_bucket,
            secure=config.minio_secure,
        )
    return LocalObjectStorage(config.local_storage_path)


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int
    total: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total}"


_RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_byte_range(value: str | None, total: int) -> ByteRange | None:
    if not value:
        return None
    match = _RANGE_PATTERN.fullmatch(value.strip())
    if not match or total <= 0:
        raise ValueError("无效 Range")
    start_text, end_text = match.groups()
    if not start_text:
        length = int(end_text or "0")
        if length <= 0:
            raise ValueError("无效 Range")
        start, end = max(0, total - length), total - 1
    else:
        start = int(start_text)
        end = min(int(end_text), total - 1) if end_text else total - 1
    if start >= total or start > end:
        raise ValueError("Range 超出文件范围")
    return ByteRange(start=start, end=end, total=total)


def validate_pdf(content: bytes, filename: str, max_bytes: int) -> None:
    if not filename.lower().endswith(".pdf"):
        raise ValueError("仅支持 PDF 文件")
    if len(content) > max_bytes:
        raise ValueError("PDF 超过大小限制")
    if len(content) < 8 or not content.startswith(b"%PDF-"):
        raise ValueError("文件头不是有效 PDF")
    if b"/Encrypt" in content[:1_000_000]:
        raise ValueError("暂不支持加密 PDF")
