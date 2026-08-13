"""执行单个预注册 RAG 方案并写出不可省略的原始证据文件。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import select

from .agent.tools import SQLLibrarySearch
from .config import Settings, settings
from .db import get_session_factory
from .evaluation_dataset import (
    FrozenEvaluationCase,
    read_frozen_cases,
    read_manifest,
    validate_dataset,
)
from .evaluation_formal_protocol import (
    FORMAL_VARIANTS,
    FormalEvaluationLock,
    sha256_file,
    verify_formal_lock,
)
from .evaluation_holdout import (
    merge_questions_and_oracle,
    read_oracle,
    read_questions,
)
from .evaluation_production import evaluate_production_cases, preflight_production_corpus
from .models import Paper, PaperChunk


@dataclass(frozen=True)
class RetrievalVariant:
    name: str
    embedding_revision: int
    embedding_input_format: Literal["chunk_text_v1", "paper_context_v2"]
    per_paper: bool
    retrieval_mode: Literal["unified", "per_paper_same", "per_paper_specific"]
    rewrite: bool
    reranker: bool


VARIANTS = {
    "production_baseline": RetrievalVariant(
        "production_baseline", 2, "paper_context_v2", True, "per_paper_same", True, False
    ),
    "plain_embedding_control": RetrievalVariant(
        "plain_embedding_control", 1, "chunk_text_v1", False, "unified", False, False
    ),
    "contextual_embedding": RetrievalVariant(
        "contextual_embedding", 2, "paper_context_v2", False, "unified", False, False
    ),
    "per_paper_retrieval": RetrievalVariant(
        "per_paper_retrieval", 2, "paper_context_v2", True, "per_paper_specific", False, False
    ),
    "weak_query_rewrite": RetrievalVariant(
        "weak_query_rewrite", 2, "paper_context_v2", False, "unified", True, False
    ),
    "multigranular_page_reranker": RetrievalVariant(
        "multigranular_page_reranker", 2, "paper_context_v2", False, "unified", False, True
    ),
    "final_combined": RetrievalVariant(
        "final_combined", 2, "paper_context_v2", True, "per_paper_specific", True, True
    ),
}
if tuple(VARIANTS) != FORMAL_VARIANTS:
    raise RuntimeError("执行器方案顺序与冻结协议不一致")


def _variant_settings(spec: RetrievalVariant) -> Settings:
    return replace(
        settings,
        embedding_index_revision=spec.embedding_revision,
        embedding_input_format=spec.embedding_input_format,
        rag_per_paper_retrieval_enabled=spec.per_paper,
        rag_weak_query_rewrite_enabled=spec.rewrite,
        rag_reranker_enabled=spec.reranker,
        rag_reranker_strategy="multigranular_v1",
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


async def _corpus_snapshot(paper_id_map: dict[str, str]) -> dict[str, object]:
    local_ids = sorted(paper_id_map.values())
    async with get_session_factory()() as session:
        papers = list(
            (
                await session.execute(
                    select(Paper).where(Paper.id.in_(local_ids)).order_by(Paper.id)
                )
            ).scalars()
        )
        rows = list(
            (
                await session.execute(
                    select(
                        PaperChunk.paper_id,
                        PaperChunk.id,
                        PaperChunk.physical_page,
                        PaperChunk.token_count,
                    )
                    .where(PaperChunk.paper_id.in_(local_ids))
                    .order_by(PaperChunk.paper_id, PaperChunk.id)
                )
            ).all()
        )
    chunk_digest = hashlib.sha256()
    for row in rows:
        chunk_digest.update("\0".join(str(value) for value in row).encode())
    index_digest = hashlib.sha256()
    index_digest.update(chunk_digest.digest())
    for paper in papers:
        index_digest.update(
            "\0".join(
                (
                    paper.id,
                    str(paper.embedding_fingerprint or ""),
                    str(paper.embedding_status),
                )
            ).encode()
        )
    return {
        "paper_count": len(papers),
        "chunk_count": len(rows),
        "chunk_snapshot_sha256": chunk_digest.hexdigest(),
        "index_snapshot_sha256": index_digest.hexdigest(),
        "paper_status_counts": {
            status: sum(paper.status == status for paper in papers)
            for status in sorted({paper.status for paper in papers})
        },
        "chunking_strategies": sorted({paper.chunking_strategy for paper in papers}),
        "embedding_fingerprints": sorted(
            {paper.embedding_fingerprint for paper in papers if paper.embedding_fingerprint}
        ),
    }


def _load_cases(
    args: argparse.Namespace,
) -> tuple[object, list[FrozenEvaluationCase], dict[str, object]]:
    manifest = read_manifest(args.manifest)
    if args.mode == "diagnostic":
        cases = [case for case in read_frozen_cases(args.cases) if case.split == "test"]
        if len(cases) != 90:
            raise RuntimeError(f"诊断集必须完整运行 90 题，当前为 {len(cases)}")
        validate_dataset(manifest, read_frozen_cases(args.cases))
        return manifest, cases, {"evaluation_status": "diagnostic_not_blind"}
    lock = FormalEvaluationLock.model_validate_json(args.lock.read_text(encoding="utf-8"))
    verification = verify_formal_lock(
        lock,
        manifest_path=args.manifest,
        questions_path=args.questions,
        oracle_path=args.oracle,
        exclusion_manifest_paths=args.exclude_manifest,
    )
    cases = merge_questions_and_oracle(read_questions(args.questions), read_oracle(args.oracle))
    validate_dataset(manifest, cases)
    if len(cases) != 100:
        raise RuntimeError("正式隐藏集必须完整运行 100 题")
    return manifest, cases, verification | {"evaluation_status": "hidden_first_formal_batch"}


async def run(args: argparse.Namespace) -> dict[str, object]:
    if args.output_dir.exists():
        raise FileExistsError("结果目录已存在，禁止覆盖正式证据")
    args.output_dir.mkdir(parents=True)
    spec = VARIANTS[args.variant]
    config = _variant_settings(spec)
    manifest, cases, protocol_status = _load_cases(args)
    required_ids = {paper_id for case in cases for paper_id in case.paper_ids}
    preflight = await preflight_production_corpus(
        manifest,
        user_email=args.user_email,
        required_paper_ids=required_ids,
        config=config,
    )
    run_manifest: dict[str, object] = {
        "schema_version": 1,
        "status": "not_executed",
        "created_at": datetime.now(UTC).isoformat(),
        "git_sha": args.git_sha,
        "docker_image_digest": args.docker_image_digest,
        "mode": args.mode,
        "variant": spec.name,
        "dataset": {
            "dataset_id": manifest.dataset_id,
            "manifest_sha256": sha256_file(args.manifest),
            "cases_or_questions_sha256": sha256_file(
                args.cases if args.mode == "diagnostic" else args.questions
            ),
            "oracle_sha256": sha256_file(args.oracle) if args.mode == "hidden" else None,
            "case_count": len(cases),
        },
        "protocol": protocol_status,
        "configuration": {
            "k": 5,
            "candidate_pool_size": config.rag_candidate_pool_size,
            "embedding_provider": config.embedding_provider,
            "embedding_model": config.embedding_model,
            "fallback_embedding_model": config.fallback_embedding_model,
            "embedding_revision": spec.embedding_revision,
            "embedding_input_format": spec.embedding_input_format,
            "retrieval_mode": spec.retrieval_mode,
            "per_paper": spec.per_paper,
            "weak_query_rewrite": spec.rewrite,
            "reranker": spec.reranker,
            "reranker_strategy": "multigranular_v1" if spec.reranker else None,
            "legacy_minilm_enabled": False,
        },
        "preflight": {
            key: value for key, value in preflight.items() if key not in {"user_id", "paper_id_map"}
        },
    }
    if preflight["status"] != "ready":
        _write_json(args.output_dir / "run_manifest.json", run_manifest)
        raise RuntimeError(f"评测预检失败：{preflight['reason']}")
    snapshot_before = await _corpus_snapshot(dict(preflight["paper_id_map"]))
    run_manifest["corpus"] = snapshot_before
    result = await evaluate_production_cases(
        cases,
        user_id=str(preflight["user_id"]),
        paper_id_map=dict(preflight["paper_id_map"]),
        retriever=SQLLibrarySearch(config=config),
        k=5,
        retrieval_mode=spec.retrieval_mode,
    )
    case_results = result.pop("case_results")
    if len(case_results) != len(cases):
        _write_json(args.output_dir / "run_manifest.json", run_manifest)
        raise RuntimeError("逐题结果数量不完整，拒绝缩小分母")
    snapshot_after = await _corpus_snapshot(dict(preflight["paper_id_map"]))
    if snapshot_after != snapshot_before:
        _write_json(args.output_dir / "run_manifest.json", run_manifest)
        raise RuntimeError("评测期间 Chunk 或 Embedding 快照发生漂移")
    _write_jsonl(args.output_dir / "per_query_results.jsonl", case_results)
    _write_json(args.output_dir / "metrics.json", result)
    run_manifest["status"] = "completed"
    run_manifest["completed_at"] = datetime.now(UTC).isoformat()
    run_manifest["artifacts"] = {
        "per_query_results.jsonl": sha256_file(args.output_dir / "per_query_results.jsonl"),
        "metrics.json": sha256_file(args.output_dir / "metrics.json"),
    }
    _write_json(args.output_dir / "run_manifest.json", run_manifest)
    return run_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="执行 PaperLeaf 预注册生产同源评测方案")
    parser.add_argument("--mode", choices=["diagnostic", "hidden"], required=True)
    parser.add_argument("--variant", choices=list(VARIANTS), required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--questions", type=Path)
    parser.add_argument("--oracle", type=Path)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--exclude-manifest", action="append", type=Path, default=[])
    parser.add_argument("--user-email", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--docker-image-digest", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    required = ["cases"] if args.mode == "diagnostic" else ["questions", "oracle", "lock"]
    missing = [field for field in required if getattr(args, field) is None]
    if missing:
        parser.error(f"{args.mode} 模式缺少参数：{missing}")
    result = asyncio.run(run(args))
    print(
        json.dumps(
            {"status": result["status"], "output": str(args.output_dir)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
