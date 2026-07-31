"""把 QASPER 人工问题转换为 PaperLeaf 页级校准集或隐藏 holdout。

构建命令只在显式调用时访问 Hugging Face 数据接口和 arXiv。PDF 与私有 oracle
必须写入仓库外目录；公开仓库只保存清单、问题和聚合结果。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .evaluation_dataset import (
    EvaluationDatasetManifest,
    EvaluationPaper,
    ExpectedEvidence,
    ExpectedEvidenceGroup,
    FrozenEvaluationCase,
    read_manifest,
)
from .evaluation_holdout import HoldoutOracleRecord, HoldoutQuestion

QASPER_DATASET = "allenai/qasper"
QASPER_LICENSE = "CC-BY-4.0"
ARXIV_NS = "http://www.w3.org/2005/Atom"
USER_AGENT = "PaperLeaf-evaluation/0.6 (+https://github.com/FrostWane/PaperLeaf)"
REFERENCE_TOKEN = re.compile(r"(?:BIBREF|TABREF|FIGREF)\d+", re.IGNORECASE)
NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class PageMatch:
    physical_page: int
    score: float
    anchor: str


@dataclass(frozen=True)
class PreparedCase:
    question: HoldoutQuestion
    oracle: HoldoutOracleRecord


def _normalized_tokens(text: str) -> list[str]:
    text = REFERENCE_TOKEN.sub(" ", text)
    text = text.replace("-\n", "").replace("\n", " ")
    return [token for token in NON_ALNUM.sub(" ", text.casefold()).split() if token]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_rank(seed: str, value: str) -> str:
    return hashlib.sha256(f"{seed}:{value}".encode()).hexdigest()


def normalize_arxiv_id(value: str) -> str:
    """把 manifest paper id、URL 或原始 QASPER id 归一为无版本 arXiv ID。"""

    normalized = value.strip().rsplit("/", 1)[-1]
    if normalized.startswith("arxiv:"):
        normalized = normalized.removeprefix("arxiv:")
    return re.sub(r"v\d+$", "", normalized)


def load_exclusion_manifests(paths: list[Path]) -> tuple[set[str], list[dict[str, Any]]]:
    """读取公开 manifest，返回论文排除集和不含本机路径的可审计摘要。"""

    excluded: set[str] = set()
    sources: list[dict[str, Any]] = []
    for path in paths:
        manifest = read_manifest(path)
        paper_ids = {
            normalize_arxiv_id(paper.arxiv_id or paper.id) for paper in manifest.papers
        }
        excluded.update(paper_ids)
        sources.append(
            {
                "dataset_id": manifest.dataset_id,
                "manifest_sha256": _sha256(path),
                "paper_count": len(paper_ids),
            }
        )
    return excluded, sources


def fetch_qasper_rows(
    split: Literal["train", "validation", "test"],
    *,
    cache_path: Path | None = None,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    if cache_path and cache_path.is_file():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    rows: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        query = urllib.parse.urlencode(
            {
                "dataset": QASPER_DATASET,
                "config": "qasper",
                "split": split,
                "offset": offset,
                "length": 100,
            }
        )
        request = urllib.request.Request(
            f"https://datasets-server.huggingface.co/rows?{query}",
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.load(response)
        batch = [item["row"] for item in payload.get("rows", [])]
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        total = int(payload.get("num_rows_total", len(rows)))

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    return rows


def resolve_arxiv_version(arxiv_id: str, *, timeout_seconds: int = 60) -> str:
    request = urllib.request.Request(
        "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({"id_list": arxiv_id}),
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        root = ET.fromstring(response.read())
    entry = root.find(f"{{{ARXIV_NS}}}entry")
    if entry is None:
        raise ValueError(f"arXiv 未返回论文：{arxiv_id}")
    entry_id = entry.findtext(f"{{{ARXIV_NS}}}id", "").rsplit("/", 1)[-1]
    if not re.fullmatch(r"\d{4}\.\d{4,5}v\d+", entry_id):
        raise ValueError(f"arXiv 版本格式异常：{entry_id}")
    return entry_id


def resolve_arxiv_versions(
    arxiv_ids: list[str],
    *,
    cache_path: Path | None = None,
    timeout_seconds: int = 90,
) -> dict[str, str]:
    cached: dict[str, str] = {}
    if cache_path and cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    pending = sorted(set(arxiv_ids) - set(cached))
    for start in range(0, len(pending), 50):
        batch = pending[start : start + 50]
        query = urllib.parse.urlencode(
            {"id_list": ",".join(batch), "start": 0, "max_results": len(batch)}
        )
        request = urllib.request.Request(
            f"https://export.arxiv.org/api/query?{query}",
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                root = ET.fromstring(response.read())
            for entry in root.findall(f"{{{ARXIV_NS}}}entry"):
                entry_id = entry.findtext(f"{{{ARXIV_NS}}}id", "").rsplit("/", 1)[-1]
                match = re.fullmatch(r"(\d{4}\.\d{4,5})v\d+", entry_id)
                if match:
                    cached[match.group(1)] = entry_id
        except Exception:
            # v1 永远是可复现的精确版本；不存在时会在 PDF 下载阶段显式失败。
            pass
        for arxiv_id in batch:
            cached.setdefault(arxiv_id, f"{arxiv_id}v1")
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(cached, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return cached


def download_arxiv_pdf(
    versioned_id: str,
    *,
    pdf_dir: Path,
    timeout_seconds: int = 120,
) -> Path:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    path = pdf_dir / f"{versioned_id}.pdf"
    if path.exists():
        if path.stat().st_size <= 10 * 1024:
            raise ValueError(f"已有 PDF 小于 10 KiB：{path}")
        return path
    request = urllib.request.Request(
        f"https://arxiv.org/pdf/{versioned_id}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        content = response.read()
    if len(content) <= 10 * 1024 or not content.startswith(b"%PDF-"):
        raise ValueError(f"arXiv 下载结果不是有效 PDF：{versioned_id}")
    path.write_bytes(content)
    time.sleep(1)
    return path


def prefetch_arxiv_pdfs(
    versioned_ids: list[str],
    *,
    pdf_dir: Path,
    workers: int = 4,
    timeout_seconds: int = 30,
) -> dict[str, str]:
    """每秒最多启动一个下载，同时重叠等待时间，避免单篇超时阻塞全批次。"""

    if workers <= 0:
        raise ValueError("workers 必须为正数")
    outcomes: dict[str, str] = {}
    futures = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for versioned_id in versioned_ids:
            path = pdf_dir / f"{versioned_id}.pdf"
            if path.is_file() and path.stat().st_size > 10 * 1024:
                outcomes[versioned_id] = "cached"
                continue
            future = executor.submit(
                download_arxiv_pdf,
                versioned_id,
                pdf_dir=pdf_dir,
                timeout_seconds=timeout_seconds,
            )
            futures[future] = versioned_id
            time.sleep(1)
        for future in as_completed(futures):
            versioned_id = futures[future]
            try:
                future.result()
                outcomes[versioned_id] = "downloaded"
            except Exception as exc:
                outcomes[versioned_id] = f"failed:{type(exc).__name__}"
    return outcomes


def extract_pdf_pages(path: Path) -> list[str]:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("QASPER 页级映射需要 PyMuPDF") from exc
    with fitz.open(path) as document:
        return [page.get_text("text") for page in document]


def match_evidence_to_page(
    evidence: str,
    pages: list[str],
    *,
    minimum_score: float = 0.67,
    normalized_pages: list[list[str]] | None = None,
) -> PageMatch | None:
    target_tokens = _normalized_tokens(evidence)
    if len(target_tokens) < 4:
        return None
    target = " ".join(target_tokens)
    target_set = set(target_tokens)
    page_tokens = normalized_pages or [_normalized_tokens(page) for page in pages]
    candidates = sorted(
        (
            (
                len(target_set & set(tokens)) / max(len(target_set), 1),
                page_number,
                tokens,
            )
            for page_number, tokens in enumerate(page_tokens, 1)
        ),
        reverse=True,
    )[:4]
    best: PageMatch | None = None
    window_size = len(target_tokens)
    for coverage, page_number, tokens in candidates:
        if coverage < 0.45:
            continue
        target_positions: dict[str, list[int]] = {}
        for index, token in enumerate(target_tokens):
            target_positions.setdefault(token, []).append(index)
        page_positions: dict[str, list[int]] = {}
        for index, token in enumerate(tokens):
            if token in target_positions:
                page_positions.setdefault(token, []).append(index)
        anchor_terms = sorted(
            page_positions,
            key=lambda token: (len(page_positions[token]), len(target_positions[token]), token),
        )[:5]
        possible_starts: set[int] = {0, max(0, len(tokens) - window_size)}
        for token in anchor_terms:
            for page_index in page_positions[token][:12]:
                for target_index in target_positions[token]:
                    estimated = page_index - target_index
                    for delta in range(-4, 5):
                        possible_starts.add(
                            max(0, min(max(0, len(tokens) - window_size), estimated + delta))
                        )
        for start in sorted(possible_starts):
            window_tokens = tokens[start : start + window_size]
            candidate = " ".join(window_tokens)
            ratio = difflib.SequenceMatcher(
                None, target, candidate, autojunk=False
            ).ratio()
            score = 0.8 * ratio + 0.2 * coverage
            anchor_tokens = window_tokens[: min(20, len(window_tokens))]
            match = PageMatch(page_number, score, " ".join(anchor_tokens))
            if best is None or (match.score, -match.physical_page) > (
                best.score,
                -best.physical_page,
            ):
                best = match
    return best if best and best.score >= minimum_score else None


def _answer_annotations(row: dict[str, Any], question_index: int) -> list[dict[str, Any]]:
    answers = row["qas"]["answers"][question_index]
    return list(answers.get("answer", []))


def _unanimous_answerability(answers: list[dict[str, Any]]) -> bool | None:
    flags = {not bool(answer.get("unanswerable")) for answer in answers}
    return next(iter(flags)) if len(flags) == 1 else None


def _evidence_snippets(answer: dict[str, Any]) -> list[str]:
    highlighted = [str(item).strip() for item in answer.get("highlighted_evidence", [])]
    evidence = highlighted or [str(item).strip() for item in answer.get("evidence", [])]
    return [
        item
        for item in evidence
        if item and not item.casefold().startswith("float selected")
    ]


def _keyword_group(answer: dict[str, Any]) -> list[str]:
    spans = []
    for span in answer.get("extractive_spans", []):
        cleaned = " ".join(_normalized_tokens(str(span)))
        if len(cleaned) >= 2:
            spans.append(cleaned)
    return list(dict.fromkeys(spans))


def prepare_qasper_case(
    *,
    row: dict[str, Any],
    question_index: int,
    versioned_id: str,
    pages: list[str],
    source_split: str,
    minimum_match_score: float = 0.67,
    normalized_pages: list[list[str]] | None = None,
    evidence_match_cache: dict[str, PageMatch | None] | None = None,
) -> PreparedCase | None:
    qas = row["qas"]
    answers = _answer_annotations(row, question_index)
    answerable = _unanimous_answerability(answers)
    if answerable is None or not answers:
        return None
    question_id = str(qas["question_id"][question_index])
    case_id = f"qasper:{question_id}"
    paper_id = f"arxiv:{versioned_id}"
    question = HoldoutQuestion(
        id=case_id,
        query=str(qas["question"][question_index]).strip(),
        paper_ids=[paper_id],
        source_dataset=f"qasper:{source_split}",
        source_question_id=question_id,
    )
    if not answerable:
        return PreparedCase(
            question=question,
            oracle=HoldoutOracleRecord(
                id=case_id, answerable=False, category="unanswerable"
            ),
        )

    evidence_groups: list[ExpectedEvidenceGroup] = []
    keyword_groups: list[list[str]] = []
    for answer in answers:
        snippets = _evidence_snippets(answer)
        if not snippets:
            continue
        by_page: dict[int, PageMatch] = {}
        valid = True
        for snippet in snippets:
            cache_key = " ".join(_normalized_tokens(snippet))
            match = (
                evidence_match_cache.get(cache_key)
                if evidence_match_cache is not None and cache_key in evidence_match_cache
                else match_evidence_to_page(
                    snippet,
                    pages,
                    minimum_score=minimum_match_score,
                    normalized_pages=normalized_pages,
                )
            )
            if evidence_match_cache is not None:
                evidence_match_cache[cache_key] = match
            if match is None:
                valid = False
                break
            current = by_page.get(match.physical_page)
            if current is None or match.score > current.score:
                by_page[match.physical_page] = match
        if not valid or not by_page:
            continue
        group = ExpectedEvidenceGroup(
            items=[
                ExpectedEvidence(
                    paper_id=paper_id,
                    physical_page=page,
                    anchor=match.anchor,
                )
                for page, match in sorted(by_page.items())
            ]
        )
        signature = tuple((item.paper_id, item.physical_page) for item in group.items)
        if signature not in {
            tuple((item.paper_id, item.physical_page) for item in existing.items)
            for existing in evidence_groups
        }:
            evidence_groups.append(group)
        keywords = _keyword_group(answer)
        if keywords and keywords not in keyword_groups:
            keyword_groups.append(keywords)
    if not evidence_groups:
        return None

    if any(len(group.items) > 1 for group in evidence_groups):
        category = "multi_page"
    elif any(answer.get("yes_no") is not None for answer in answers):
        category = "yes_no"
    elif any(answer.get("extractive_spans") for answer in answers):
        category = "extractive"
    else:
        category = "free_form"
    return PreparedCase(
        question=question,
        oracle=HoldoutOracleRecord(
            id=case_id,
            answerable=True,
            acceptable_evidence_groups=evidence_groups,
            acceptable_answer_keyword_groups=keyword_groups,
            category=category,
        ),
    )


def _paper_from_pdf(row: dict[str, Any], versioned_id: str, path: Path) -> EvaluationPaper:
    pages = extract_pdf_pages(path)
    return EvaluationPaper(
        id=f"arxiv:{versioned_id}",
        title=str(row["title"]).strip(),
        arxiv_id=versioned_id,
        source_url=f"https://arxiv.org/abs/{versioned_id}",
        pdf_url=f"https://arxiv.org/pdf/{versioned_id}",
        filename=path.name,
        sha256=_sha256(path),
        page_count=len(pages),
    )


def _write_jsonl(path: Path, records: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            record.model_dump_json(exclude_defaults=True) for record in records
        )
        + "\n",
        encoding="utf-8",
    )


def build_qasper_dataset(
    *,
    rows: list[dict[str, Any]],
    source_split: Literal["train", "validation", "test"],
    mode: Literal["calibration", "holdout"],
    dataset_id: str,
    selection_seed: str,
    target_answerable: int,
    target_unanswerable: int,
    minimum_papers: int,
    pdf_dir: Path,
    output_dir: Path,
    oracle_output: Path | None,
    created_at: str,
    maximum_papers: int = 120,
    minimum_match_score: float = 0.67,
    version_cache_path: Path | None = None,
    version_policy: Literal["v1", "api-latest"] = "v1",
    prefetch_workers: int = 0,
    offline_pdfs_only: bool = False,
    excluded_arxiv_ids: set[str] | None = None,
    exclusion_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if mode == "holdout" and oracle_output is None:
        raise ValueError("holdout 模式必须把 oracle 写入仓库外路径")
    excluded = {normalize_arxiv_id(item) for item in excluded_arxiv_ids or set()}
    eligible_rows = [
        row for row in rows if normalize_arxiv_id(str(row["id"])) not in excluded
    ]
    ordered_rows = sorted(
        eligible_rows, key=lambda row: _stable_rank(selection_seed, str(row["id"]))
    )
    candidate_ids = [str(row["id"]) for row in ordered_rows[:maximum_papers]]
    versions = (
        {arxiv_id: f"{arxiv_id}v1" for arxiv_id in candidate_ids}
        if version_policy == "v1"
        else resolve_arxiv_versions(candidate_ids, cache_path=version_cache_path)
    )
    prefetch_outcomes: dict[str, str] = {}
    if prefetch_workers:
        prefetch_outcomes = prefetch_arxiv_pdfs(
            list(versions.values()), pdf_dir=pdf_dir, workers=prefetch_workers
        )
    selected: list[PreparedCase] = []
    papers: list[EvaluationPaper] = []
    answerable_count = unanswerable_count = 0
    failures: Counter[str] = Counter()
    max_cases_per_paper = max(
        1,
        (target_answerable + target_unanswerable + minimum_papers - 1)
        // minimum_papers,
    )

    for paper_index, row in enumerate(ordered_rows[:maximum_papers], 1):
        if (
            answerable_count >= target_answerable
            and unanswerable_count >= target_unanswerable
            and len(papers) >= minimum_papers
        ):
            break
        raw_id = str(row["id"])
        try:
            versioned_id = versions[raw_id]
            pdf_path = pdf_dir / f"{versioned_id}.pdf"
            if offline_pdfs_only:
                if not pdf_path.is_file() or pdf_path.stat().st_size <= 10 * 1024:
                    raise FileNotFoundError(pdf_path)
            else:
                pdf_path = download_arxiv_pdf(versioned_id, pdf_dir=pdf_dir)
            pages = extract_pdf_pages(pdf_path)
            normalized_pages = [_normalized_tokens(page) for page in pages]
        except Exception as exc:
            failures["paper_fetch_or_parse"] += 1
            print(
                f"[{paper_index}/{min(maximum_papers, len(ordered_rows))}] "
                f"skip {raw_id}: {type(exc).__name__}",
                flush=True,
            )
            continue
        prepared_for_paper: list[PreparedCase] = []
        evidence_match_cache: dict[str, PageMatch | None] = {}
        question_indices = sorted(
            range(len(row["qas"]["question"])),
            key=lambda index: _stable_rank(
                selection_seed, str(row["qas"]["question_id"][index])
            ),
        )
        for index in question_indices:
            try:
                prepared = prepare_qasper_case(
                    row=row,
                    question_index=index,
                    versioned_id=versioned_id,
                    pages=pages,
                    source_split=source_split,
                    minimum_match_score=minimum_match_score,
                    normalized_pages=normalized_pages,
                    evidence_match_cache=evidence_match_cache,
                )
            except Exception:
                failures["question_conversion"] += 1
                continue
            if prepared is None:
                failures["ambiguous_or_unmapped"] += 1
                continue
            if prepared.oracle.answerable:
                if answerable_count >= target_answerable:
                    continue
                answerable_count += 1
            else:
                if unanswerable_count >= target_unanswerable:
                    continue
                unanswerable_count += 1
            prepared_for_paper.append(prepared)
            if len(prepared_for_paper) >= max_cases_per_paper:
                break
        if prepared_for_paper:
            selected.extend(prepared_for_paper)
            papers.append(_paper_from_pdf(row, versioned_id, pdf_path))
        print(
            f"[{paper_index}/{min(maximum_papers, len(ordered_rows))}] {versioned_id}: "
            f"accepted={len(prepared_for_paper)}, papers={len(papers)}, "
            f"answerable={answerable_count}/{target_answerable}, "
            f"unanswerable={unanswerable_count}/{target_unanswerable}",
            flush=True,
        )

    if answerable_count != target_answerable or unanswerable_count != target_unanswerable:
        raise RuntimeError(
            "QASPER 配额未满足："
            f"answerable={answerable_count}/{target_answerable}, "
            f"unanswerable={unanswerable_count}/{target_unanswerable}, "
            f"failures={dict(failures)}"
        )
    if len(papers) < minimum_papers:
        raise RuntimeError(f"论文数量不足：{len(papers)}/{minimum_papers}")

    category_counts = Counter(item.oracle.category for item in selected)
    manifest = EvaluationDatasetManifest(
        dataset_id=dataset_id,
        version=created_at,
        created_at=created_at,
        annotation_license=QASPER_LICENSE,
        paper_count=len(papers),
        case_count=len(selected),
        answerable_count=answerable_count,
        unanswerable_count=unanswerable_count,
        category_counts=dict(sorted(category_counts.items())),
        papers=papers,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    if mode == "calibration":
        cases = [
            FrozenEvaluationCase(
                id=item.question.id,
                query=item.question.query,
                paper_ids=item.question.paper_ids,
                answerable=item.oracle.answerable,
                acceptable_evidence_groups=item.oracle.acceptable_evidence_groups,
                acceptable_answer_keyword_groups=item.oracle.acceptable_answer_keyword_groups,
                category=item.oracle.category,
                split="dev",
            )
            for item in selected
        ]
        _write_jsonl(output_dir / "cases.jsonl", cases)
    else:
        _write_jsonl(output_dir / "questions.jsonl", [item.question for item in selected])
        _write_jsonl(oracle_output, [item.oracle for item in selected])

    receipt = {
        "dataset_id": dataset_id,
        "source_dataset": QASPER_DATASET,
        "source_split": source_split,
        "selection_seed": selection_seed,
        "mode": mode,
        "paper_count": len(papers),
        "case_count": len(selected),
        "answerable_count": answerable_count,
        "unanswerable_count": unanswerable_count,
        "category_counts": dict(sorted(category_counts.items())),
        "conversion_failures": dict(sorted(failures.items())),
        "minimum_match_score": minimum_match_score,
        "version_policy": version_policy,
        "prefetch": dict(sorted(Counter(prefetch_outcomes.values()).items())),
        "source_paper_count": len(rows),
        "excluded_source_paper_count": len(rows) - len(eligible_rows),
        "exclusion_sources": exclusion_sources or [],
    }
    (output_dir / "build-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 PaperLeaf QASPER 页级评测集")
    parser.add_argument("--source-split", choices=("train", "validation", "test"), required=True)
    parser.add_argument("--mode", choices=("calibration", "holdout"), required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--selection-seed", required=True)
    parser.add_argument("--target-answerable", type=int, required=True)
    parser.add_argument("--target-unanswerable", type=int, required=True)
    parser.add_argument("--minimum-papers", type=int, required=True)
    parser.add_argument("--maximum-papers", type=int, default=120)
    parser.add_argument("--minimum-match-score", type=float, default=0.67)
    parser.add_argument("--pdf-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--oracle-output", type=Path)
    parser.add_argument("--source-cache", type=Path)
    parser.add_argument("--version-cache", type=Path)
    parser.add_argument(
        "--version-policy", choices=("v1", "api-latest"), default="v1"
    )
    parser.add_argument("--prefetch-workers", type=int, default=0)
    parser.add_argument("--offline-pdfs-only", action="store_true")
    parser.add_argument(
        "--exclude-manifest",
        action="append",
        default=[],
        type=Path,
        help="排除已用于校准或其他评测的数据集论文；可重复提供",
    )
    parser.add_argument("--created-at", required=True)
    args = parser.parse_args()
    rows = fetch_qasper_rows(args.source_split, cache_path=args.source_cache)
    excluded_arxiv_ids, exclusion_sources = load_exclusion_manifests(
        args.exclude_manifest
    )
    receipt = build_qasper_dataset(
        rows=rows,
        source_split=args.source_split,
        mode=args.mode,
        dataset_id=args.dataset_id,
        selection_seed=args.selection_seed,
        target_answerable=args.target_answerable,
        target_unanswerable=args.target_unanswerable,
        minimum_papers=args.minimum_papers,
        maximum_papers=args.maximum_papers,
        minimum_match_score=args.minimum_match_score,
        version_cache_path=args.version_cache,
        version_policy=args.version_policy,
        prefetch_workers=args.prefetch_workers,
        offline_pdfs_only=args.offline_pdfs_only,
        pdf_dir=args.pdf_dir,
        output_dir=args.output_dir,
        oracle_output=args.oracle_output,
        created_at=args.created_at,
        excluded_arxiv_ids=excluded_arxiv_ids,
        exclusion_sources=exclusion_sources,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
