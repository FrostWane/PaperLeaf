"""把答案语义支持核验拆成小批次，并保守聚合部分结果。

该模块不依赖具体模型，也不改变 Agent 主链。调用方负责提供单批 grader；
grader 看到的主张编号从 1 重新开始，本模块再映射回整篇答案的全局编号。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from .answer_quality import AnswerClaim, extract_answer_claims
from .citations import Evidence
from .retrieval_quality import AnswerSupport

SupportBatchGrader = Callable[[str, str, list[Evidence]], Awaitable[AnswerSupport]]
_UNAVAILABLE_REASON_CODES = {"grader_unavailable", "not_configured"}


@dataclass(frozen=True)
class EvidenceSupportBatch:
    """一个可独立核验的主张批次。"""

    ordinal: int
    claims: tuple[AnswerClaim, ...]
    evidence: tuple[Evidence, ...]
    answer: str

    @property
    def global_claim_indices(self) -> tuple[int, ...]:
        return tuple(claim.index for claim in self.claims)


@dataclass(frozen=True)
class EvidenceSupportBatchFailure:
    """不含用户正文的单批失败审计信息。"""

    ordinal: int
    reason_code: str
    error_type: str | None = None


@dataclass(frozen=True)
class BatchedEvidenceSupportResult:
    """批处理聚合结果；``support`` 可直接交给现有答案质量门禁。"""

    support: AnswerSupport
    batch_count: int
    succeeded_batch_count: int
    failed_batches: tuple[EvidenceSupportBatchFailure, ...]


def _render_batch_answer(claims: Sequence[AnswerClaim]) -> str:
    lines: list[str] = []
    for claim in claims:
        markers = "".join(f"[chunk:{chunk_id}]" for chunk_id in claim.citation_ids)
        suffix = f" {markers}" if markers else ""
        lines.append(f"- {claim.text.rstrip('。！？!?；; ')}{suffix}。")
    return "\n".join(lines)


def build_evidence_support_batches(
    answer: str,
    evidence: Sequence[Evidence],
    *,
    batch_size: int = 4,
) -> tuple[EvidenceSupportBatch, ...]:
    """按主张顺序分批，每批只携带该批实际引用的合法证据。"""

    if not 1 <= batch_size <= 8:
        raise ValueError("batch_size 必须在 1 到 8 之间")
    claims = extract_answer_claims(answer)
    evidence_by_id = {item.chunk_id: item for item in evidence}
    batches: list[EvidenceSupportBatch] = []
    for start in range(0, len(claims), batch_size):
        batch_claims = tuple(claims[start : start + batch_size])
        cited_ids = {
            chunk_id
            for claim in batch_claims
            for chunk_id in claim.citation_ids
            if chunk_id in evidence_by_id
        }
        # 遵循召回证据原顺序，避免集合遍历导致同一输入产生不同 prompt。
        seen_evidence_ids: set[str] = set()
        batch_evidence = tuple(
            item
            for item in evidence
            if item.chunk_id in cited_ids
            and not (item.chunk_id in seen_evidence_ids or seen_evidence_ids.add(item.chunk_id))
        )
        batches.append(
            EvidenceSupportBatch(
                ordinal=len(batches) + 1,
                claims=batch_claims,
                evidence=batch_evidence,
                answer=_render_batch_answer(batch_claims),
            )
        )
    return tuple(batches)


def _global_supported_indices(
    batch: EvidenceSupportBatch,
    support: AnswerSupport,
) -> tuple[int, ...]:
    local_indices = set(support.supported_claim_indices)
    if support.supported is True:
        local_indices = set(range(1, len(batch.claims) + 1))
    return tuple(
        claim.index
        for local_index, claim in enumerate(batch.claims, start=1)
        if local_index in local_indices
    )


async def grade_evidence_support_batches(
    query: str,
    answer: str,
    evidence: Sequence[Evidence],
    grader: SupportBatchGrader,
    *,
    batch_size: int = 4,
    max_concurrency: int = 2,
) -> BatchedEvidenceSupportResult:
    """并发核验小批主张，失败批次不覆盖已经通过的全局主张编号。

    单批 grader 的 ``supported_claim_indices`` 必须使用该批从 1 开始的局部
    编号。全批不可用时返回 ``grader_unavailable``；只有部分批次不可用时
    返回 ``partial_grader_unavailable``，并保留其他批次已确认的主张。
    """

    if max_concurrency < 1:
        raise ValueError("max_concurrency 必须为正数")
    batches = build_evidence_support_batches(answer, evidence, batch_size=batch_size)
    if not batches:
        return BatchedEvidenceSupportResult(
            support=AnswerSupport(False, 0.0, "no_answer_claims"),
            batch_count=0,
            succeeded_batch_count=0,
            failed_batches=(),
        )

    semaphore = asyncio.Semaphore(max_concurrency)

    async def run_one(
        batch: EvidenceSupportBatch,
    ) -> tuple[EvidenceSupportBatch, AnswerSupport | None, BaseException | None]:
        try:
            async with semaphore:
                support = await grader(query, batch.answer, list(batch.evidence))
            return batch, support, None
        except Exception as exc:  # noqa: BLE001 - 单批故障必须与其他批次隔离
            return batch, None, exc

    outcomes = await asyncio.gather(*(run_one(batch) for batch in batches))
    failures: list[EvidenceSupportBatchFailure] = []
    supported_indices: set[int] = set()
    confidences: list[float] = []
    available_supports: list[AnswerSupport] = []
    for batch, support, error in outcomes:
        if error is not None:
            failures.append(
                EvidenceSupportBatchFailure(
                    ordinal=batch.ordinal,
                    reason_code="grader_error",
                    error_type=type(error).__name__,
                )
            )
            continue
        assert support is not None
        if support.supported is None or support.reason_code in _UNAVAILABLE_REASON_CODES:
            failures.append(
                EvidenceSupportBatchFailure(
                    ordinal=batch.ordinal,
                    reason_code=support.reason_code,
                )
            )
            continue
        available_supports.append(support)
        supported_indices.update(_global_supported_indices(batch, support))
        if support.confidence is not None:
            confidences.append(support.confidence)

    claim_count = sum(len(batch.claims) for batch in batches)
    ordered_supported = tuple(sorted(supported_indices))
    supported_count = len(ordered_supported)
    available_count = len(available_supports)
    if available_count == 0:
        aggregate = AnswerSupport(
            supported=False,
            confidence=0.0,
            reason_code="grader_unavailable",
            claim_count=claim_count,
            supported_claim_count=0,
            support_coverage=0.0,
        )
    elif failures:
        aggregate = AnswerSupport(
            supported=False,
            confidence=min(confidences) if confidences else 0.0,
            reason_code="partial_grader_unavailable",
            claim_count=claim_count,
            supported_claim_count=supported_count,
            support_coverage=supported_count / claim_count,
            supported_claim_indices=ordered_supported,
        )
    else:
        all_supported = all(item.supported is True for item in available_supports)
        aggregate = AnswerSupport(
            supported=all_supported,
            confidence=min(confidences) if confidences else 0.0,
            reason_code="answer_supported" if all_supported else "answer_not_supported",
            claim_count=claim_count,
            supported_claim_count=supported_count,
            support_coverage=supported_count / claim_count,
            supported_claim_indices=ordered_supported,
        )
    return BatchedEvidenceSupportResult(
        support=aggregate,
        batch_count=len(batches),
        succeeded_batch_count=available_count,
        failed_batches=tuple(failures),
    )
