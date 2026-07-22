"""RAG 离线评测协议与指标计算。

该模块只计算可核验的分子、分母和比率，不内置数据，也不生成虚构成绩。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import BaseModel, Field


class EvaluationCase(BaseModel):
    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    paper_ids: list[str] = Field(min_length=1)
    answerable: bool
    expected_pages: list[int] = Field(default_factory=list)
    expected_chunk_ids: list[str] = Field(default_factory=list)
    expected_answer_keywords: list[str] = Field(default_factory=list)
    category: str


class CitationPrediction(BaseModel):
    chunk_id: str
    physical_page: int = Field(ge=1)


class EvaluationPrediction(BaseModel):
    case_id: str
    answer: str
    abstained: bool
    retrieved_chunk_ids: list[str]
    citations: list[CitationPrediction] = Field(default_factory=list)
    latency_ms: int = Field(ge=0)


def _metric(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def evaluate(
    cases: list[EvaluationCase], predictions: list[EvaluationPrediction], *, k: int = 5
) -> dict:
    if k <= 0:
        raise ValueError("k 必须为正数")
    by_id = {prediction.case_id: prediction for prediction in predictions}
    if len(by_id) != len(predictions):
        raise ValueError("预测中存在重复 case_id")
    missing = [case.id for case in cases if case.id not in by_id]
    unknown = sorted(set(by_id) - {case.id for case in cases})
    if missing or unknown:
        raise ValueError(f"评测 ID 不匹配：missing={missing}, unknown={unknown}")

    retrieved_expected = total_expected_chunks = 0
    correct_citation_pages = total_citations = 0
    covered_answers = answerable_count = 0
    wrong_unanswerable = unanswerable_count = 0
    keyword_correct = keyword_cases = 0
    illegal_citations = 0
    latencies: list[int] = []

    for case in cases:
        prediction = by_id[case.id]
        top_k = set(prediction.retrieved_chunk_ids[:k])
        expected_chunks = set(case.expected_chunk_ids)
        retrieved_expected += len(top_k & expected_chunks)
        total_expected_chunks += len(expected_chunks)
        latencies.append(prediction.latency_ms)

        retrieved_all = set(prediction.retrieved_chunk_ids)
        illegal_citations += sum(
            1 for citation in prediction.citations if citation.chunk_id not in retrieved_all
        )

        if case.answerable:
            answerable_count += 1
            expected_pages = set(case.expected_pages)
            correct_pages = sum(
                1 for citation in prediction.citations if citation.physical_page in expected_pages
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
        "retrieval_recall_at_k": {"k": k, **_metric(retrieved_expected, total_expected_chunks)},
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
