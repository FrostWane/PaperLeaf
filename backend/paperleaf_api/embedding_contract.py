"""Embedding 向量空间的版本契约。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EmbeddingContract:
    provider: str
    model: str
    dimensions: int
    revision: int
    fingerprint: str


def contract_fingerprint(model: str, dimensions: int, revision: int) -> str:
    raw = f"{model.strip()}|{int(dimensions)}|{int(revision)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def configured_embedding_contract(config: Any, router: Any) -> EmbeddingContract | None:
    """返回当前可查询的唯一向量空间；维度未知时拒绝猜测。"""

    dimensions = int(getattr(config, "embedding_dimensions", 0) or 0)
    revision = int(getattr(config, "embedding_index_revision", 1) or 1)
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
            fingerprint=contract_fingerprint(model, dimensions, revision),
        )
    return None


def vector_matches_contract(vector: list[float], contract: EmbeddingContract) -> bool:
    return len(vector) == contract.dimensions
