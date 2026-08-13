"""Embedding 向量空间的版本契约。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

EMBEDDING_INPUT_FORMAT = "paper_context_v2"


@dataclass(frozen=True)
class EmbeddingContract:
    provider: str
    model: str
    dimensions: int
    revision: int
    input_format: str
    fingerprint: str


def contract_fingerprint(
    model: str,
    dimensions: int,
    revision: int,
    *,
    input_format: str = EMBEDDING_INPUT_FORMAT,
) -> str:
    # 输入模板属于向量空间契约。即使旧部署仍显式配置 revision=1，升级后也不能
    # 把“纯正文”旧向量与“标题+章节+页码+正文”新向量混在同一索引中。
    raw = f"{model.strip()}|{int(dimensions)}|{int(revision)}|{input_format.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def configured_embedding_contract(config: Any, router: Any) -> EmbeddingContract | None:
    """返回当前可查询的唯一向量空间；维度未知时拒绝猜测。"""

    dimensions = int(getattr(config, "embedding_dimensions", 0) or 0)
    revision = int(getattr(config, "embedding_index_revision", 1) or 1)
    input_format = str(
        getattr(config, "embedding_input_format", EMBEDDING_INPUT_FORMAT)
        or EMBEDDING_INPUT_FORMAT
    )
    if dimensions <= 0:
        return None
    providers = list(getattr(router, "providers", []) or [])
    legacy_provider = getattr(router, "provider", None)
    if not providers and legacy_provider is not None:
        providers = [legacy_provider]
    requested_provider = str(getattr(config, "embedding_provider", "auto") or "auto")
    for provider in providers:
        provider_name = str(getattr(provider, "name", "unknown"))
        if requested_provider != "auto" and provider_name != requested_provider:
            continue
        supports = getattr(provider, "supports", None)
        if callable(supports) and not supports("embedding"):
            continue
        model = str(getattr(provider, "embedding_model", "")).strip()
        if not model:
            continue
        return EmbeddingContract(
            provider=provider_name,
            model=model,
            dimensions=dimensions,
            revision=revision,
            input_format=input_format,
            fingerprint=contract_fingerprint(
                model, dimensions, revision, input_format=input_format
            ),
        )
    return None


def vector_matches_contract(vector: list[float], contract: EmbeddingContract) -> bool:
    return len(vector) == contract.dimensions
