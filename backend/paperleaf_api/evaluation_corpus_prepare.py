"""把冻结评测清单的 arXiv 论文导入本地 PaperLeaf，并严格等待索引就绪。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .evaluation_dataset import read_manifest
from .evaluation_production import preflight_production_corpus


class CorpusPreparer:
    def __init__(self, base_url: str, timeout_seconds: int) -> None:
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(90), follow_redirects=False
        )
        self.timeout_seconds = timeout_seconds
        self.csrf = ""

    async def close(self) -> None:
        await self.client.aclose()

    async def login(self) -> None:
        email = os.getenv("PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL")
        password = os.getenv("PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD")
        if not email or not password:
            raise RuntimeError("缺少管理员环境变量")
        response = await self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        response.raise_for_status()
        self.csrf = self.client.cookies.get(settings.csrf_cookie) or ""
        if not self.csrf:
            raise RuntimeError("登录后未获得 CSRF Cookie")

    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}

    async def post_with_retry(
        self, path: str, *, payload: dict[str, Any], attempts: int = 3
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self.client.post(
                    path,
                    headers=self.headers(),
                    json=payload,
                )
                if response.status_code not in {429, 502, 503, 504}:
                    return response
                last_error = RuntimeError(f"HTTP {response.status_code}")
            except httpx.RequestError as exc:
                last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(2**attempt)
        raise RuntimeError(f"请求 {path} 连续 {attempts} 次失败") from last_error

    async def upload_with_retry(
        self, path: Path, *, title: str, attempts: int = 3
    ) -> httpx.Response:
        content = path.read_bytes()
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self.client.post(
                    "/api/v1/papers",
                    headers=self.headers(),
                    files={"file": (path.name, content, "application/pdf")},
                    data={"title": title},
                )
                if response.status_code not in {429, 502, 503, 504}:
                    return response
                last_error = RuntimeError(f"HTTP {response.status_code}")
            except httpx.RequestError as exc:
                last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(2**attempt)
        raise RuntimeError(f"上传 {path.name} 连续 {attempts} 次失败") from last_error

    async def papers(self) -> list[dict[str, Any]]:
        response = await self.client.get("/api/v1/papers")
        response.raise_for_status()
        return list(response.json())

    async def ensure_manifest(
        self,
        manifest_path: Path,
        *,
        force_reindex: bool = False,
        pdf_dirs: list[Path] | None = None,
    ) -> dict[str, Any]:
        manifest = read_manifest(manifest_path)
        existing = await self.papers()
        existing_sha = {str(item.get("sha256")): item for item in existing}
        existing_arxiv = {str(item.get("arxiv_id")): item for item in existing}
        submitted = 0
        uploaded = 0
        for paper in manifest.papers:
            if paper.sha256 in existing_sha or paper.arxiv_id in existing_arxiv:
                continue
            local_pdf = next(
                (
                    directory / paper.filename
                    for directory in pdf_dirs or []
                    if (directory / paper.filename).is_file()
                ),
                None,
            )
            if local_pdf is not None:
                if local_pdf.stat().st_size <= 10 * 1024:
                    raise RuntimeError(f"本地 PDF 过小：{paper.filename}")
                if hashlib.sha256(local_pdf.read_bytes()).hexdigest() != paper.sha256:
                    raise RuntimeError(f"本地 PDF SHA-256 不匹配：{paper.filename}")
                response = await self.upload_with_retry(local_pdf, title=paper.title)
                uploaded += int(response.status_code == 201)
            else:
                response = await self.post_with_retry(
                    "/api/v1/discover/arxiv/import",
                    payload={"arxiv_id": paper.arxiv_id},
                )
            if response.status_code not in {201, 409}:
                raise RuntimeError(
                    f"导入 {paper.arxiv_id} 失败：HTTP {response.status_code}"
                )
            submitted += int(response.status_code == 201)

        reindexed = 0
        if force_reindex:
            refreshed = await self.papers()
            by_sha = {str(item.get("sha256")): item for item in refreshed}
            targets = [
                str(by_sha[paper.sha256]["id"])
                for paper in manifest.papers
                if paper.sha256 in by_sha
                and by_sha[paper.sha256].get("status") == "ready"
            ]
            if len(targets) != manifest.paper_count:
                raise RuntimeError("强制重索引前必须确保清单论文全部已 ready")
            for start in range(0, len(targets), 100):
                batch = targets[start : start + 100]
                response = await self.post_with_retry(
                    "/api/v1/papers/bulk",
                    payload={"paper_ids": batch, "action": "reindex"},
                )
                response.raise_for_status()
                reindexed += int(response.json().get("affected", 0))

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            current = await self.papers()
            by_sha = {str(item.get("sha256")): item for item in current}
            missing = [paper.arxiv_id for paper in manifest.papers if paper.sha256 not in by_sha]
            failed = [
                paper.arxiv_id
                for paper in manifest.papers
                if paper.sha256 in by_sha and by_sha[paper.sha256].get("status") == "failed"
            ]
            if failed:
                raise RuntimeError(f"评测论文处理失败：{failed}")
            pending = [
                paper.arxiv_id
                for paper in manifest.papers
                if paper.sha256 in by_sha
                and (
                    by_sha[paper.sha256].get("status") != "ready"
                    or by_sha[paper.sha256].get("embedding_status") != "ready"
                    or by_sha[paper.sha256].get("embedding_index_revision")
                    != settings.embedding_index_revision
                )
            ]
            if not missing and not pending:
                email = os.getenv("PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL") or ""
                preflight = await preflight_production_corpus(
                    manifest, user_email=email
                )
                if preflight["status"] != "ready":
                    raise RuntimeError(
                        "语料 API 状态已完成但生产预检未通过："
                        f"{preflight.get('reason')}"
                    )
                return {
                    "status": "ready",
                    "dataset_id": manifest.dataset_id,
                    "paper_count": manifest.paper_count,
                    "submitted": submitted,
                    "uploaded": uploaded,
                    "reindexed": reindexed,
                    "embedding_revision": settings.embedding_index_revision,
                    "embedding_input_format": settings.embedding_input_format,
                }
            await asyncio.sleep(5)
        raise TimeoutError(f"评测语料未在期限内就绪：missing={missing}, pending={pending}")


async def _run(args: argparse.Namespace) -> list[dict[str, Any]]:
    preparer = CorpusPreparer(args.base_url, args.timeout_seconds)
    try:
        await preparer.login()
        return [
            await preparer.ensure_manifest(
                path,
                force_reindex=args.force_reindex,
                pdf_dirs=args.pdf_dir,
            )
            for path in args.manifest
        ]
    finally:
        await preparer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="准备 PaperLeaf 冻结评测语料")
    parser.add_argument("--manifest", action="append", required=True, type=Path)
    parser.add_argument("--base-url", default="http://api:8000")
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--force-reindex", action="store_true")
    parser.add_argument(
        "--pdf-dir",
        action="append",
        type=Path,
        default=[],
        help="优先从精确 PDF 缓存上传；可重复提供。缺失时回退 arXiv 导入。",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
