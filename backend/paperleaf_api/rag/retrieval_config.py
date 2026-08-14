"""AgentRun 的可复现检索配置快照。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..embedding_contract import configured_embedding_contract
from ..model_runtime import build_model_router

_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def resolve_git_sha() -> tuple[str, bool, str]:
    """优先使用构建注入 SHA，本地源码运行时再读取当前仓库 HEAD。"""

    configured = os.getenv("PAPERLEAF_GIT_SHA", "").strip().lower()
    if _FULL_GIT_SHA_RE.fullmatch(configured):
        return configured, True, "environment"
    repository_root = Path(__file__).resolve().parents[3]
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        detected = result.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        detected = ""
    if _FULL_GIT_SHA_RE.fullmatch(detected):
        return detected, True, "git"
    return "unknown", False, "unavailable"


def freeze_retrieval_config(config: Any) -> dict[str, Any]:
    """冻结不含密钥和端点的完整检索行为配置，并生成稳定指纹。"""

    router = build_model_router(config)
    contract = configured_embedding_contract(config, router)
    git_sha, git_sha_verified, git_sha_source = resolve_git_sha()
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "git_sha": git_sha,
        "git_sha_verified": git_sha_verified,
        "git_sha_source": git_sha_source,
        "candidate_pool_size": int(config.rag_candidate_pool_size),
        "per_paper_retrieval_enabled": bool(config.rag_per_paper_retrieval_enabled),
        "per_paper_candidate_limit": int(config.rag_per_paper_candidate_limit),
        "weak_query_rewrite_enabled": bool(config.rag_weak_query_rewrite_enabled),
        "query_rewrite_max_queries": int(config.rag_query_rewrite_max_queries),
        "reranker_enabled": bool(config.rag_reranker_enabled),
        "reranker_strategy": str(config.rag_reranker_strategy),
        "reranker_model": str(config.rag_reranker_model),
        "reranker_candidate_limit": int(config.rag_reranker_candidate_limit),
        "reranker_timeout_seconds": float(config.rag_reranker_timeout_seconds),
        "rrf_rank_constant": 60,
        "page_dedup_enabled": True,
        "cross_paper_merge_policy": "paper_subquery_1_1_1_plus_2_v1",
        "evidence_min_confidence": float(config.evidence_min_confidence),
        "evidence_min_vector_score": float(config.evidence_min_vector_score),
        "evidence_min_lexical_coverage": float(config.evidence_min_lexical_coverage),
        "answerability_enabled": bool(config.answerability_enabled),
        "answerability_min_confidence": float(config.answerability_min_confidence),
        "embedding": {
            "enabled": bool(config.embedding_enabled and contract is not None),
            "provider": (
                contract.provider if contract is not None else str(config.embedding_provider)
            ),
            "model": contract.model if contract is not None else str(config.embedding_model),
            "dimensions": (
                contract.dimensions if contract is not None else config.embedding_dimensions
            ),
            "index_revision": (
                contract.revision if contract is not None else int(config.embedding_index_revision)
            ),
            "input_format": (
                contract.input_format
                if contract is not None
                else str(config.embedding_input_format)
            ),
            "fingerprint": contract.fingerprint if contract is not None else None,
        },
    }
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return snapshot


@dataclass(frozen=True)
class RetrievalConfigOverlay:
    """只覆盖已冻结的检索字段，未冻结的运行时连接配置仍来自基础 Settings。"""

    base: Any
    values: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        frozen_name = {
            "rag_candidate_pool_size": "candidate_pool_size",
            "rag_per_paper_retrieval_enabled": "per_paper_retrieval_enabled",
            "rag_per_paper_candidate_limit": "per_paper_candidate_limit",
            "rag_weak_query_rewrite_enabled": "weak_query_rewrite_enabled",
            "rag_query_rewrite_max_queries": "query_rewrite_max_queries",
            "rag_reranker_enabled": "reranker_enabled",
            "rag_reranker_strategy": "reranker_strategy",
            "rag_reranker_model": "reranker_model",
            "rag_reranker_candidate_limit": "reranker_candidate_limit",
            "rag_reranker_timeout_seconds": "reranker_timeout_seconds",
            "rag_rrf_rank_constant": "rrf_rank_constant",
            "evidence_min_confidence": "evidence_min_confidence",
            "evidence_min_vector_score": "evidence_min_vector_score",
            "evidence_min_lexical_coverage": "evidence_min_lexical_coverage",
            "answerability_enabled": "answerability_enabled",
            "answerability_min_confidence": "answerability_min_confidence",
        }.get(name)
        if frozen_name is not None and frozen_name in self.values:
            return self.values[frozen_name]
        embedding = self.values.get("embedding")
        embedding_name = {
            "embedding_enabled": "enabled",
            "embedding_provider": "provider",
            "embedding_model": "model",
            "embedding_dimensions": "dimensions",
            "embedding_index_revision": "index_revision",
            "embedding_input_format": "input_format",
        }.get(name)
        if embedding_name is not None and isinstance(embedding, dict):
            return embedding.get(embedding_name)
        return getattr(self.base, name)


def retrieval_config_overlay(base: Any, snapshot: dict[str, Any] | None) -> Any:
    if not snapshot:
        return base
    if int(snapshot.get("schema_version", 0)) != 1 or not snapshot.get("fingerprint"):
        raise ValueError("AgentRun 检索配置快照无效")
    candidate = dict(snapshot)
    fingerprint = str(candidate.pop("fingerprint"))
    canonical = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != fingerprint:
        raise ValueError("AgentRun 检索配置快照指纹不一致")
    return RetrievalConfigOverlay(base=base, values=snapshot)
