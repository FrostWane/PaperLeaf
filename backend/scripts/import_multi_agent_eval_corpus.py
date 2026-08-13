"""通过真实 PaperLeaf API 导入多 Agent 评测语料。

脚本只读取凭证，不打印凭证；重复论文会被记录为 skipped。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _dataset_arxiv_ids(cases_path: Path) -> list[str]:
    identifiers: set[str] = set()
    for line in cases_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        for paper_id in case.get("scope_paper_ids", []):
            if paper_id.startswith("arxiv:"):
                identifiers.add(paper_id.removeprefix("arxiv:"))
    return sorted(identifiers)


def main() -> int:
    parser = argparse.ArgumentParser(description="导入多 Agent 评测所需的 arXiv 论文")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--env-file", type=Path, default=Path("../.env"))
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("evaluation/multi-agent-compare-v1/cases.jsonl"),
    )
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    args = parser.parse_args()

    env = _read_env(args.env_file.resolve())
    email = env.get("PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL", "")
    password = env.get("PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not email or not password:
        raise SystemExit("环境文件缺少管理员凭证")

    arxiv_ids = _dataset_arxiv_ids(args.cases.resolve())
    results = {"created": [], "skipped": [], "failed": []}
    with httpx.Client(base_url=args.base_url, timeout=180.0) as client:
        response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        response.raise_for_status()
        csrf = client.cookies.get("paperleaf_csrf")
        if not csrf:
            raise SystemExit("登录成功但未取得 CSRF Cookie")

        for index, arxiv_id in enumerate(arxiv_ids, start=1):
            try:
                response = client.post(
                    "/api/v1/discover/arxiv/import",
                    json={"arxiv_id": arxiv_id},
                    headers={"X-CSRF-Token": csrf},
                )
            except httpx.TimeoutException:
                results["failed"].append(
                    {"arxiv_id": arxiv_id, "status": 0, "detail": "客户端等待超时，需重跑确认"}
                )
                print(
                    f"[{index:02d}/{len(arxiv_ids):02d}] {arxiv_id}: 等待超时，继续下一篇",
                    flush=True,
                )
                continue
            except httpx.RequestError as error:
                results["failed"].append(
                    {
                        "arxiv_id": arxiv_id,
                        "status": 0,
                        "detail": f"连接中断：{type(error).__name__}，需重跑确认",
                    }
                )
                print(
                    f"[{index:02d}/{len(arxiv_ids):02d}] {arxiv_id}: 连接中断，继续下一篇",
                    flush=True,
                )
                continue
            if response.status_code == 201:
                results["created"].append(arxiv_id)
                outcome = "已创建"
            elif response.status_code == 409:
                results["skipped"].append(arxiv_id)
                outcome = "已存在"
            else:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                detail = (
                    payload.get("detail", response.text[:160])
                    if isinstance(payload, dict)
                    else response.text[:160]
                )
                results["failed"].append(
                    {
                        "arxiv_id": arxiv_id,
                        "status": response.status_code,
                        "detail": detail,
                    }
                )
                outcome = f"失败({response.status_code})"
            print(f"[{index:02d}/{len(arxiv_ids):02d}] {arxiv_id}: {outcome}", flush=True)
            if index < len(arxiv_ids):
                time.sleep(max(0.0, args.delay_seconds))

    print(json.dumps({key: len(value) for key, value in results.items()}, ensure_ascii=False))
    return 1 if results["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
