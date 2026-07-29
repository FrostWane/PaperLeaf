"""RAG 离线评测协议与指标计算。

该模块只计算可核验的分子、分母和比率，不内置数据，也不生成虚构成绩。
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .evaluation_dataset import ExpectedEvidence


class EvaluationCase(BaseModel):
    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    paper_ids: list[str] = Field(min_length=1)
    answerable: bool
    expected_pages: list[int] = Field(default_factory=list)
    expected_chunk_ids: list[str] = Field(default_factory=list)
    expected_evidence: list[ExpectedEvidence] = Field(default_factory=list)
    expected_answer_keywords: list[str] = Field(default_factory=list)
    category: str
    split: str = "all"


class CitationPrediction(BaseModel):
    chunk_id: str
    paper_id: str | None = None
    physical_page: int = Field(ge=1)


class RetrievedEvidencePrediction(BaseModel):
    chunk_id: str
    paper_id: str
    physical_page: int = Field(ge=1)
    score: float | None = None


class EvaluationPrediction(BaseModel):
    case_id: str
    answer: str
    abstained: bool
    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    retrieved_evidence: list[RetrievedEvidencePrediction] = Field(default_factory=list)
    citations: list[CitationPrediction] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    latency_ms: int = Field(ge=0)


def _metric(numerator: int | float, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def _prediction_chunk_ids(prediction: EvaluationPrediction) -> list[str]:
    return prediction.retrieved_chunk_ids or [
        evidence.chunk_id for evidence in prediction.retrieved_evidence
    ]


def _expected_pairs(case: EvaluationCase) -> set[tuple[str, int]]:
    return {(item.paper_id, item.physical_page) for item in case.expected_evidence}


def _retrieved_pairs(prediction: EvaluationPrediction, *, k: int) -> list[tuple[str, int]]:
    return [(item.paper_id, item.physical_page) for item in prediction.retrieved_evidence[:k]]


def _evaluate_core(
    cases: list[EvaluationCase], by_id: dict[str, EvaluationPrediction], *, k: int
) -> dict[str, Any]:
    retrieved_expected = total_expected = 0
    reciprocal_rank_sum = reciprocal_rank_cases = 0
    correct_citation_pages = total_citations = 0
    covered_answers = answerable_count = 0
    wrong_unanswerable = unanswerable_count = 0
    keyword_correct = keyword_cases = 0
    illegal_citations = 0
    latencies: list[int] = []

    for case in cases:
        prediction = by_id[case.id]
        chunk_ids = _prediction_chunk_ids(prediction)
        expected_pairs = _expected_pairs(case)
        if expected_pairs and prediction.retrieved_evidence:
            ranked_pairs = _retrieved_pairs(prediction, k=k)
            top_k_pairs = set(ranked_pairs)
            retrieved_expected += len(top_k_pairs & expected_pairs)
            total_expected += len(expected_pairs)
            reciprocal_rank_cases += 1
            reciprocal_rank_sum += next(
                (
                    1 / rank
                    for rank, evidence_pair in enumerate(ranked_pairs, start=1)
                    if evidence_pair in expected_pairs
                ),
                0,
            )
        else:
            top_k_chunks = set(chunk_ids[:k])
            expected_chunks = set(case.expected_chunk_ids)
            retrieved_expected += len(top_k_chunks & expected_chunks)
            total_expected += len(expected_chunks)
            if expected_chunks:
                reciprocal_rank_cases += 1
                reciprocal_rank_sum += next(
                    (
                        1 / rank
                        for rank, chunk_id in enumerate(chunk_ids[:k], start=1)
                        if chunk_id in expected_chunks
                    ),
                    0,
                )
        latencies.append(prediction.latency_ms)

        retrieved_all = set(chunk_ids)
        illegal_citations += sum(
            1 for citation in prediction.citations if citation.chunk_id not in retrieved_all
        )

        if case.answerable:
            answerable_count += 1
            expected_pages = set(case.expected_pages)
            expected_evidence = _expected_pairs(case)
            correct_pages = sum(
                1
                for citation in prediction.citations
                if (
                    citation.paper_id is not None
                    and (citation.paper_id, citation.physical_page) in expected_evidence
                )
                or (
                    citation.paper_id is None
                    and citation.physical_page
                    in (expected_pages or {page for _, page in expected_evidence})
                )
            )
            correct_citation_pages += correct_pages
            total_citations += len(prediction.citations)
            covered_answers += int(correct_pages > 0)
            if case.expected_answer_keywords:
                keyword_cases += 1
                normalized = prediction.answer.casefold()
                keyword_correct += int(
                    all(
                        keyword.casefold() in normalized
                        for keyword in case.expected_answer_keywords
                    )
                )
        else:
            unanswerable_count += 1
            wrong_unanswerable += int(not prediction.abstained)

    sorted_latency = sorted(latencies)
    p95_index = max(0, min(len(sorted_latency) - 1, int(len(sorted_latency) * 0.95) - 1))
    return {
        "case_count": len(cases),
        "answerable_count": answerable_count,
        "unanswerable_count": unanswerable_count,
        "retrieval_recall_at_k": {"k": k, **_metric(retrieved_expected, total_expected)},
        "retrieval_mrr_at_k": {
            "k": k,
            **_metric(reciprocal_rank_sum, reciprocal_rank_cases),
        },
        "citation_page_accuracy": _metric(correct_citation_pages, total_citations),
        "citation_coverage": _metric(covered_answers, answerable_count),
        "answer_keyword_accuracy": _metric(keyword_correct, keyword_cases),
        "unanswerable_wrong_answer_rate": _metric(wrong_unanswerable, unanswerable_count),
        "illegal_citation_count": illegal_citations,
        "latency_ms": {
            "median": sorted_latency[len(sorted_latency) // 2] if sorted_latency else None,
            "p95": sorted_latency[p95_index] if sorted_latency else None,
        },
    }


def evaluate(
    cases: list[EvaluationCase], predictions: list[EvaluationPrediction], *, k: int = 5
) -> dict[str, Any]:
    if k <= 0:
        raise ValueError("k 必须为正数")
    if not cases:
        raise ValueError("cases 不能为空")
    case_ids = [case.id for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("用例中存在重复 id")
    by_id = {prediction.case_id: prediction for prediction in predictions}
    if len(by_id) != len(predictions):
        raise ValueError("预测中存在重复 case_id")
    missing = [case.id for case in cases if case.id not in by_id]
    unknown = sorted(set(by_id) - set(case_ids))
    if missing or unknown:
        raise ValueError(f"评测 ID 不匹配：missing={missing}, unknown={unknown}")

    result = _evaluate_core(cases, by_id, k=k)
    for field, output_name in (("split", "by_split"), ("category", "by_category")):
        grouped: dict[str, list[EvaluationCase]] = defaultdict(list)
        for case in cases:
            grouped[getattr(case, field)].append(case)
        result[output_name] = {
            name: _evaluate_core(group, by_id, k=k) for name, group in sorted(grouped.items())
        }
    return result


def _read_jsonl(path: Path, model: type[BaseModel]) -> list[BaseModel]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except Exception as exc:
            raise ValueError(f"{path}:{line_number} 不是合法记录") from exc
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="计算 PaperLeaf RAG 离线指标")
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("-k", type=int, default=5)
    args = parser.parse_args()
    cases = _read_jsonl(args.cases, EvaluationCase)
    predictions = _read_jsonl(args.predictions, EvaluationPrediction)
    result = evaluate(cases, predictions, k=args.k)
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(content + "\n", encoding="utf-8")
    else:
        print(content)


if __name__ == "__main__":
    main()
