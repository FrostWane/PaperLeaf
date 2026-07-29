"""Reciprocal Rank Fusion，无模型依赖、可复现实验。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RankedHit:
    id: str
    score: float
    payload: object | None = None


def reciprocal_rank_fusion(
    rankings: list[list[RankedHit]], *, rank_constant: int = 60, limit: int = 10
) -> list[RankedHit]:
    if rank_constant <= 0 or limit <= 0:
        raise ValueError("rank_constant 与 limit 必须为正数")
    totals: dict[str, float] = {}
    payloads: dict[str, object | None] = {}
    best_position: dict[str, int] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for position, hit in enumerate(ranking, start=1):
            if hit.id in seen:
                continue
            seen.add(hit.id)
            totals[hit.id] = totals.get(hit.id, 0.0) + 1.0 / (rank_constant + position)
            payloads.setdefault(hit.id, hit.payload)
            best_position[hit.id] = min(best_position.get(hit.id, position), position)

    ordered = sorted(totals, key=lambda item: (-totals[item], best_position[item], item))[:limit]
    return [RankedHit(id=item, score=totals[item], payload=payloads[item]) for item in ordered]
