"""可复现 RAG 评测集的清单、标注与文件完整性校验。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class EvaluationPaper(BaseModel):
    id: str = Field(pattern=r"^arxiv:\d{4}\.\d{4,5}v\d+$")
    title: str = Field(min_length=1)
    arxiv_id: str = Field(pattern=r"^\d{4}\.\d{4,5}v\d+$")
    source_url: HttpUrl
    pdf_url: HttpUrl
    filename: str = Field(pattern=r"^[0-9.]+v\d+\.pdf$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)
    redistribution: Literal["external-link-only"] = "external-link-only"


class ChunkingSpec(BaseModel):
    target_tokens: int = Field(default=700, gt=0)
    overlap_tokens: int = Field(default=100, ge=0)

    @model_validator(mode="after")
    def validate_overlap(self) -> ChunkingSpec:
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens 必须小于 target_tokens")
        return self


class EvaluationDatasetManifest(BaseModel):
    schema_version: Literal[1] = 1
    dataset_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    created_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    annotation_license: Literal["Apache-2.0", "CC-BY-4.0"] = "Apache-2.0"
    pdf_distribution: Literal["external-links-only"] = "external-links-only"
    paper_count: int = Field(ge=1)
    case_count: int = Field(ge=1)
    answerable_count: int = Field(ge=0)
    unanswerable_count: int = Field(ge=0)
    category_counts: dict[str, int]
    chunking: ChunkingSpec = Field(default_factory=ChunkingSpec)
    papers: list[EvaluationPaper] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_declared_counts(self) -> EvaluationDatasetManifest:
        if self.paper_count != len(self.papers):
            raise ValueError("paper_count 与 papers 数量不一致")
        if self.answerable_count + self.unanswerable_count != self.case_count:
            raise ValueError("可回答与不可回答数量之和必须等于 case_count")
        if sum(self.category_counts.values()) != self.case_count:
            raise ValueError("category_counts 之和必须等于 case_count")
        ids = [paper.id for paper in self.papers]
        if len(set(ids)) != len(ids):
            raise ValueError("论文清单存在重复 id")
        return self


class ExpectedEvidence(BaseModel):
    paper_id: str
    physical_page: int = Field(ge=1)
    anchor: str = Field(min_length=12)


class ExpectedEvidenceGroup(BaseModel):
    """一组可共同回答问题的证据页；多个组之间是可接受的替代标注。"""

    items: list[ExpectedEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_pages(self) -> ExpectedEvidenceGroup:
        pairs = [(item.paper_id, item.physical_page) for item in self.items]
        if len(set(pairs)) != len(pairs):
            raise ValueError("同一证据组不能重复论文与物理页")
        return self


class FrozenEvaluationCase(BaseModel):
    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    paper_ids: list[str] = Field(min_length=1)
    answerable: bool
    expected_evidence: list[ExpectedEvidence] = Field(default_factory=list)
    acceptable_evidence_groups: list[ExpectedEvidenceGroup] = Field(default_factory=list)
    expected_answer_keywords: list[str] = Field(default_factory=list)
    acceptable_answer_keyword_groups: list[list[str]] = Field(default_factory=list)
    category: str = Field(min_length=1)
    split: Literal["dev", "test", "holdout"]

    @model_validator(mode="after")
    def validate_answerability(self) -> FrozenEvaluationCase:
        if self.answerable and not (
            self.expected_evidence or self.acceptable_evidence_groups
        ):
            raise ValueError("可回答问题必须提供证据或可接受证据组")
        if not self.answerable and (
            self.expected_evidence or self.acceptable_evidence_groups
        ):
            raise ValueError("不可回答问题不能包含证据")
        if any(not group for group in self.acceptable_answer_keyword_groups):
            raise ValueError("答案关键词组不能为空")
        if len(set(self.paper_ids)) != len(self.paper_ids):
            raise ValueError("paper_ids 不能重复")
        return self


class DatasetValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


def case_evidence_items(case: FrozenEvaluationCase) -> list[ExpectedEvidence]:
    """返回旧式标注与替代证据组的去重并集，用于完整性校验。"""

    items = [*case.expected_evidence]
    items.extend(
        item for group in case.acceptable_evidence_groups for item in group.items
    )
    unique: dict[tuple[str, int, str], ExpectedEvidence] = {}
    for item in items:
        unique[(item.paper_id, item.physical_page, _normalized(item.anchor))] = item
    return list(unique.values())


def _normalized(text: str) -> str:
    text = text.replace("-\n", "").replace("\u00ad", "")
    text = re.sub(r"[^\w]+", " ", text.casefold(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _normalized_ascii(text: str) -> str:
    """与 QASPER 页匹配器一致的 ASCII 退化形式，用于处理 PDF 数学修饰符。"""

    text = text.replace("-\n", "").replace("\u00ad", "")
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_manifest(path: Path) -> EvaluationDatasetManifest:
    return EvaluationDatasetManifest.model_validate_json(path.read_text(encoding="utf-8"))


def read_frozen_cases(path: Path) -> list[FrozenEvaluationCase]:
    records: list[FrozenEvaluationCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(FrozenEvaluationCase.model_validate_json(line))
        except Exception as exc:
            raise ValueError(f"{path}:{line_number} 不是合法冻结标注") from exc
    return records


def validate_dataset(
    manifest: EvaluationDatasetManifest,
    cases: list[FrozenEvaluationCase],
    *,
    pdf_dir: Path | None = None,
) -> dict[str, object]:
    errors: list[str] = []
    paper_by_id = {paper.id: paper for paper in manifest.papers}
    case_ids = [case.id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        errors.append("cases 存在重复 id")
    if len(cases) != manifest.case_count:
        errors.append(f"case_count 声明 {manifest.case_count}，实际 {len(cases)}")

    answerable_count = sum(case.answerable for case in cases)
    if answerable_count != manifest.answerable_count:
        errors.append(f"answerable_count 声明 {manifest.answerable_count}，实际 {answerable_count}")
    unanswerable_count = len(cases) - answerable_count
    if unanswerable_count != manifest.unanswerable_count:
        errors.append(
            f"unanswerable_count 声明 {manifest.unanswerable_count}，实际 {unanswerable_count}"
        )
    category_counts = Counter(case.category for case in cases)
    if dict(sorted(category_counts.items())) != dict(sorted(manifest.category_counts.items())):
        errors.append(
            "category_counts 不一致："
            f"声明={dict(sorted(manifest.category_counts.items()))}，"
            f"实际={dict(sorted(category_counts.items()))}"
        )

    paper_case_counts: Counter[str] = Counter()
    for case in cases:
        unknown_scope = sorted(set(case.paper_ids) - set(paper_by_id))
        if unknown_scope:
            errors.append(f"{case.id} 引用了未知论文：{unknown_scope}")
        for paper_id in case.paper_ids:
            paper_case_counts[paper_id] += 1
        for evidence in case_evidence_items(case):
            paper = paper_by_id.get(evidence.paper_id)
            if not paper:
                errors.append(f"{case.id} 的证据论文不存在：{evidence.paper_id}")
                continue
            if evidence.paper_id not in case.paper_ids:
                errors.append(f"{case.id} 的证据论文不在问题 scope 中：{evidence.paper_id}")
            if evidence.physical_page > paper.page_count:
                errors.append(
                    f"{case.id} 的页码 {evidence.physical_page} 超过 {paper.id} 的 "
                    f"{paper.page_count} 页"
                )

    page_texts: dict[tuple[str, int], str] = {}
    ascii_page_texts: dict[tuple[str, int], str] = {}
    if pdf_dir is not None:
        try:
            import fitz
        except ImportError as exc:  # pragma: no cover - 安装包已声明 PyMuPDF
            raise RuntimeError("校验 PDF 需要 PyMuPDF") from exc

        for paper in manifest.papers:
            path = pdf_dir / paper.filename
            if not path.is_file():
                errors.append(f"缺少 PDF：{path}")
                continue
            actual_hash = _sha256(path)
            if actual_hash != paper.sha256:
                errors.append(f"{paper.id} SHA-256 不匹配：{actual_hash}")
                continue
            try:
                with fitz.open(path) as document:
                    if document.page_count != paper.page_count:
                        errors.append(
                            f"{paper.id} 页数声明 {paper.page_count}，实际 {document.page_count}"
                        )
                    for page_number in range(document.page_count):
                        raw_text = document.load_page(page_number).get_text("text")
                        key = (paper.id, page_number + 1)
                        page_texts[key] = _normalized(raw_text)
                        ascii_page_texts[key] = _normalized_ascii(raw_text)
            except Exception as exc:
                errors.append(f"{paper.id} 无法解析：{type(exc).__name__}")

        for case in cases:
            for evidence in case_evidence_items(case):
                page_text = page_texts.get((evidence.paper_id, evidence.physical_page), "")
                ascii_page_text = ascii_page_texts.get(
                    (evidence.paper_id, evidence.physical_page), ""
                )
                normalized_anchor = _normalized(evidence.anchor)
                ascii_anchor = _normalized_ascii(evidence.anchor)
                if normalized_anchor not in page_text and (
                    not ascii_anchor or ascii_anchor not in ascii_page_text
                ):
                    errors.append(
                        f"{case.id} 的证据锚点未出现在 "
                        f"{evidence.paper_id} p.{evidence.physical_page}"
                    )

    if errors:
        raise DatasetValidationError(errors)
    return {
        "dataset_id": manifest.dataset_id,
        "version": manifest.version,
        "paper_count": len(manifest.papers),
        "case_count": len(cases),
        "answerable_count": answerable_count,
        "unanswerable_count": unanswerable_count,
        "category_counts": dict(sorted(category_counts.items())),
        "split_counts": dict(sorted(Counter(case.split for case in cases).items())),
        "paper_case_counts": dict(sorted(paper_case_counts.items())),
        "pdf_files_verified": len(manifest.papers) if pdf_dir is not None else 0,
        "evidence_anchors_verified": (
            sum(len(case_evidence_items(case)) for case in cases)
            if pdf_dir is not None
            else 0
        ),
    }


def write_validation_report(report: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="校验 PaperLeaf 冻结 RAG 数据集")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--pdf-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate_dataset(
        read_manifest(args.manifest),
        read_frozen_cases(args.cases),
        pdf_dir=args.pdf_dir,
    )
    if args.output:
        write_validation_report(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
