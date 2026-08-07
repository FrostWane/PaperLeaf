#!/usr/bin/env python3
"""用完全虚构的 PDF 验证真实 RAG、持久 Trace 与管理员聚合。

脚本不会读取现有文献。只有脚本内的 SyntheticDTA 文本会发送给当前配置的模型。
默认在验收后清理测试论文和会话；传入 ``--keep`` 可暂时保留，便于检查管理员页面。
"""

from __future__ import annotations

import argparse
import json
import secrets
import time

import fitz
from smoke_compose import (
    PaperLeafClient,
    SmokeConfig,
    SmokeFailure,
    multipart_pdf,
    wait_until,
)


def build_synthetic_pdf() -> bytes:
    pages = [
        (
            "SyntheticDTA: a fictional observability paper\n\n"
            "SyntheticDTA studies drug-target affinity prediction. The research asks whether "
            "sequence-only models can estimate binding affinity without molecular complexes."
        ),
        (
            "Method\n\nThe method uses separate one-dimensional convolutional encoders for "
            "SMILES drug strings and amino-acid protein sequences. Their representations are "
            "concatenated and passed to a regression head."
        ),
        (
            "Results and limitations\n\nOn fictional benchmarks, concordance improves from 0.71 "
            "to 0.79. The study does not evaluate unseen protein families and provides no "
            "prospective laboratory validation."
        ),
    ]
    document = fitz.open()
    try:
        for text in pages:
            page = document.new_page()
            page.insert_textbox(fitz.Rect(72, 72, 540, 720), text, fontsize=12)
        return document.tobytes(garbage=4, deflate=True)
    finally:
        document.close()


def run(config: SmokeConfig, *, keep: bool) -> dict:
    client = PaperLeafClient(config)
    paper_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    client.json(
        "POST",
        "/api/v1/auth/login",
        payload={"email": config.admin_email, "password": config.admin_password},
        sensitive=True,
    )
    try:
        multipart, content_type = multipart_pdf(
            build_synthetic_pdf(), "SyntheticDTA RAG observability smoke"
        )
        _, upload_content, _ = client.request(
            "POST",
            "/api/v1/papers",
            body=multipart,
            headers={"Content-Type": content_type, "X-CSRF-Token": client.csrf_token()},
            expected={201},
        )
        paper_id = str(json.loads(upload_content)["id"])

        def paper_ready() -> tuple[bool, str | None]:
            _, paper, _ = client.json("GET", f"/api/v1/papers/{paper_id}")
            status = paper.get("status") if isinstance(paper, dict) else None
            if status == "failed":
                raise SmokeFailure("SyntheticDTA PDF 解析失败")
            return status in {"ready", "partial"}, status

        wait_until(client, "SyntheticDTA PDF 索引", paper_ready)
        _, chat, _ = client.json(
            "POST",
            "/api/v1/chat/sessions",
            payload={
                "title": "RAG 可观测性验收",
                "type": "paper",
                "paper_id": paper_id,
            },
            csrf=True,
            expected={201},
        )
        if not isinstance(chat, dict):
            raise SmokeFailure("会话创建响应不合法")
        session_id = str(chat["id"])
        question = (
            "请用中文说明 SyntheticDTA 如何使用 sequence convolutional encoders "
            "预测 drug-target affinity，并给出页码引用。"
        )
        message_body = json.dumps(
            {"content": question, "web_enabled": False}, ensure_ascii=False
        ).encode("utf-8")
        _, submission_content, _ = client.request(
            "POST",
            f"/api/v1/chat/sessions/{session_id}/messages",
            body=message_body,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": client.csrf_token(),
                "Idempotency-Key": f"rag-observability-{secrets.token_hex(12)}",
            },
            expected={202},
        )
        run_id = str(json.loads(submission_content)["run_id"])

        def run_finished() -> tuple[bool, dict]:
            _, value, _ = client.json("GET", f"/api/v1/agent/runs/{run_id}")
            if not isinstance(value, dict):
                raise SmokeFailure("Agent Run 响应不合法")
            return value.get("status") in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }, value

        run = wait_until(client, "SyntheticDTA RAG 回答", run_finished)
        _, report, _ = client.json("GET", "/api/v1/admin/observability?window=24h")
        if not isinstance(run, dict) or not isinstance(report, dict):
            raise SmokeFailure("RAG 验收结果不合法")
        totals = report.get("totals", {})
        if int(totals.get("telemetry_runs", 0)) < 1:
            raise SmokeFailure("管理员聚合没有采集新 RAG Trace")
        return {
            "paper_id": paper_id,
            "session_id": session_id,
            "run_id": run_id,
            "run_status": run.get("status"),
            "error_code": run.get("error_code"),
            "duration_ms": run.get("duration_ms"),
            "telemetry_runs": totals.get("telemetry_runs"),
            "telemetry_coverage": totals.get("telemetry_coverage"),
            "kept_for_ui_review": keep,
        }
    finally:
        if not keep and session_id:
            try:
                client.json(
                    "DELETE",
                    f"/api/v1/chat/sessions/{session_id}",
                    csrf=True,
                    expected={204, 404},
                )
            except Exception:
                pass
        if not keep and paper_id:
            try:
                client.json(
                    "DELETE",
                    f"/api/v1/papers/{paper_id}",
                    csrf=True,
                    expected={202, 404},
                )
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留合成论文和会话用于界面验收")
    args = parser.parse_args()
    started_at = time.perf_counter()
    try:
        result = run(SmokeConfig.from_environment(), keep=args.keep)
    except SmokeFailure as exc:
        print(f"RAG 可观测性冒烟失败：{exc}")
        return 1
    result["wall_seconds"] = round(time.perf_counter() - started_at, 2)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
