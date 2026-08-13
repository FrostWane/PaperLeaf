"""把冻结评测清单的 arXiv 论文导入本地 PaperLeaf，并严格等待索引就绪。"""

from __future__ import annotations

import argparse
import asyncio
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

    async def papers(self) -> list[dict[str, Any]]:
        response = await self.client.get("/api/v1/papers")
        response.raise_for_status()
        return list(response.json())

    async def ensure_manifest(self, manifest_path: Path) -> dict[str, Any]:
        manifest = read_manifest(manifest_path)
        existing = await self.papers()
        existing_sha = {str(item.get("sha256")): item for item in existing}
        existing_arxiv = {str(item.get("arxiv_id")): item for item in existing}
        submitted = 0
        for paper in manifest.papers:
            if paper.sha256 in existing_sha or paper.arxiv_id in existing_arxiv:
                continue
            response: httpx.Response | None = None
            for attempt in range(3):
                response = await self.client.post(
                    "/api/v1/discover/arxiv/import",
                    headers=self.headers(),
                    json={"arxiv_id": paper.arxiv_id},
                )
                if response.status_code not in {429, 502, 503, 504}:
                    break
                await asyncio.sleep(2**attempt)
            assert response is not None
            if response.status_code not in {201, 409}:
                raise RuntimeError(
                    f"导入 {paper.arxiv_id} 失败：HTTP {response.status_code}"
                )
            submitted += int(response.status_code == 201)

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
                    or by_sha[paper.sha256].get("embedding_index_revision") != 2
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
                }
            await asyncio.sleep(5)
        raise TimeoutError(f"评测语料未在期限内就绪：missing={missing}, pending={pending}")


async def _run(args: argparse.Namespace) -> list[dict[str, Any]]:
    preparer = CorpusPreparer(args.base_url, args.timeout_seconds)
    try:
        await preparer.login()
        return [await preparer.ensure_manifest(path) for path in args.manifest]
    finally:
        await preparer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="准备 PaperLeaf 冻结评测语料")
    parser.add_argument("--manifest", action="append", required=True, type=Path)
    parser.add_argument("--base-url", default="http://api:8000")
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
