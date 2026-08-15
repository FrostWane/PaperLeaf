#!/usr/bin/env python3
"""对已启动的隔离 Compose 环境执行真实 API/Worker/SSE 闭环。"""

from __future__ import annotations

import argparse
import json
import secrets
import urllib.request
from pathlib import Path

from smoke_compose import (
    PaperLeafClient,
    SmokeConfig,
    SmokeFailure,
    build_minimal_pdf,
    multipart_pdf,
    wait_until,
)


def _parse_sse(stream, *, stop_after: int | None = None) -> list[dict]:
    events: list[dict] = []
    current: dict[str, str] = {}
    while True:
        raw = stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if current.get("data"):
                body = json.loads(current["data"])
                events.append(body)
                if stop_after and len(events) >= stop_after:
                    break
            current = {}
            continue
        if line.startswith(":"):
            continue
        key, _, value = line.partition(":")
        current[key] = value.lstrip()
    return events


def _stream_events(
    client: PaperLeafClient,
    run_id: str,
    *,
    after: int = 0,
    stop_after: int | None = None,
) -> list[dict]:
    headers = {"Accept": "text/event-stream"}
    if after:
        headers["Last-Event-ID"] = str(after)
    request = urllib.request.Request(
        f"{client.config.api_url}/api/v1/agent/runs/{run_id}/events", headers=headers
    )
    with client.opener.open(request, timeout=client.config.timeout_seconds) as response:
        return _parse_sse(response, stop_after=stop_after)


def run(config: SmokeConfig, state_output: Path) -> None:
    client = PaperLeafClient(config)
    wait_until(
        client,
        "API 与 Worker ready",
        lambda: (
            (lambda status, payload: (
                status == 200 and payload.get("agent_ready") is True,
                payload,
            ))(
                *client.json("GET", "/ready", expected={200, 503})[:2]
            )
        ),
    )
    _, login, _ = client.json(
        "POST",
        "/api/v1/auth/login",
        payload={"email": config.admin_email, "password": config.admin_password},
        sensitive=True,
    )
    if not isinstance(login, dict) or login.get("role") != "admin":
        raise SmokeFailure("管理员登录失败")
    for path in (
        "/api/v1/admin/users",
        "/api/v1/admin/jobs",
        "/api/v1/admin/observability?window=7d",
        "/api/v1/admin/harness/metrics?window=7d",
    ):
        _, value, _ = client.json("GET", path)
        if not isinstance(value, dict | list):
            raise SmokeFailure(f"管理员流程返回无效数据：{path}")

    marker = secrets.token_hex(8)
    user_email = f"smoke-user-{marker}@paperleaf.invalid"
    temporary_password = secrets.token_urlsafe(24)
    user_password = secrets.token_urlsafe(28)
    _, created_user, _ = client.json(
        "POST",
        "/api/v1/admin/users",
        payload={
            "email": user_email,
            "temporary_password": temporary_password,
            "role": "user",
        },
        csrf=True,
        expected={201},
        sensitive=True,
    )
    if not isinstance(created_user, dict) or created_user.get("role") != "user":
        raise SmokeFailure("管理员创建普通用户失败")
    client.json("POST", "/api/v1/auth/logout", csrf=True, expected={204})
    _, user_login, _ = client.json(
        "POST",
        "/api/v1/auth/login",
        payload={"email": user_email, "password": temporary_password},
        sensitive=True,
    )
    if not isinstance(user_login, dict) or user_login.get("must_change_password") is not True:
        raise SmokeFailure("普通用户首次登录没有要求修改临时密码")
    _, changed_user, _ = client.json(
        "POST",
        "/api/v1/auth/change-password",
        payload={"current_password": temporary_password, "new_password": user_password},
        csrf=True,
        sensitive=True,
    )
    if not isinstance(changed_user, dict) or changed_user.get("must_change_password") is not False:
        raise SmokeFailure("普通用户修改密码后状态不正确")
    client.json("GET", "/api/v1/admin/users", expected={403})
    user_id = str(changed_user["id"])

    pdf = build_minimal_pdf(marker)
    multipart, content_type = multipart_pdf(pdf, f"PaperLeaf smoke {marker}")
    _, upload_content, _ = client.request(
        "POST",
        "/api/v1/papers",
        body=multipart,
        headers={"Content-Type": content_type, "X-CSRF-Token": client.csrf_token()},
        expected={201},
    )
    paper = json.loads(upload_content)
    paper_id = str(paper["id"])

    def paper_ready():
        _, value, _ = client.json("GET", f"/api/v1/papers/{paper_id}")
        status = value.get("status") if isinstance(value, dict) else None
        if status == "failed":
            raise SmokeFailure("PDF Worker 解析失败")
        return status == "ready", status

    wait_until(client, "PDF 解析和索引", paper_ready)
    _, session, _ = client.json(
        "POST",
        "/api/v1/chat/sessions",
        payload={"title": "隔离 smoke", "type": "paper", "paper_id": paper_id},
        csrf=True,
        expected={201},
    )
    session_id = str(session["id"])
    body = json.dumps(
        {"content": f"PaperLeaf smoke {marker} 这篇文献说明了什么？", "web_enabled": False},
        ensure_ascii=False,
    ).encode("utf-8")
    _, submission_content, _ = client.request(
        "POST",
        f"/api/v1/chat/sessions/{session_id}/messages",
        body=body,
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": client.csrf_token(),
            "Idempotency-Key": f"smoke-{marker}",
        },
        expected={202},
    )
    run_id = str(json.loads(submission_content)["run_id"])

    first_events = _stream_events(client, run_id, stop_after=2)
    if not first_events:
        raise SmokeFailure("SSE 没有返回持久事件")
    cursor = int(first_events[-1]["sequence"])
    resumed_events = _stream_events(client, run_id, after=cursor)
    if any(int(item["sequence"]) <= cursor for item in resumed_events):
        raise SmokeFailure("Last-Event-ID 重连重复返回了旧事件")

    def run_completed():
        _, value, _ = client.json("GET", f"/api/v1/agent/runs/{run_id}")
        status = value.get("status") if isinstance(value, dict) else None
        if status in {"failed", "cancelled", "interrupted"}:
            raise SmokeFailure(f"Agent Run 进入非法终态：{status}")
        return status == "completed", value

    completed = wait_until(client, "Agent Run completed", run_completed)
    if not str(completed.get("answer", "")).strip():
        raise SmokeFailure("Agent 回答为空")
    citations = completed.get("citations") if isinstance(completed.get("citations"), list) else []
    if not citations or int(citations[0].get("physical_page", 0) or 0) < 1:
        raise SmokeFailure("Agent 回答缺少合法物理页引用")
    citation_page = int(citations[0]["physical_page"])

    status_code, partial, headers = client.request(
        "GET",
        f"/api/v1/papers/{paper_id}/file",
        headers={"Range": "bytes=0-7"},
        expected={206},
    )
    content_range = next(
        (value for key, value in headers.items() if key.casefold() == "content-range"), ""
    )
    if (
        status_code != 206
        or not partial.startswith(b"%PDF-")
        or not content_range.startswith("bytes 0-7/")
    ):
        raise SmokeFailure("PDF Range 契约失败")

    client.json("POST", "/api/v1/auth/logout", csrf=True, expected={204})
    client.json(
        "POST",
        "/api/v1/auth/login",
        payload={"email": config.admin_email, "password": config.admin_password},
        sensitive=True,
    )
    _, observability, _ = client.json("GET", "/api/v1/admin/observability?window=7d")
    totals = observability.get("totals", {}) if isinstance(observability, dict) else {}
    if int(totals.get("telemetry_runs", 0) or 0) < 1:
        raise SmokeFailure("管理员 RAG 质量接口没有采集刚完成的 Agent Run")

    state_output.parent.mkdir(parents=True, exist_ok=True)
    state_output.write_text(
        json.dumps(
            {
                "paper_id": paper_id,
                "user_id": user_id,
                "user_email": user_email,
                "user_password": user_password,
                "run_id": run_id,
                "citation_page": citation_page,
                "initial_event_sequences": [int(item["sequence"]) for item in first_events],
                "resumed_event_sequences": [int(item["sequence"]) for item in resumed_events],
                "run_status": completed["status"],
                "answer_nonempty": True,
                "citation_count": len(citations),
                "range_ok": True,
                "admin_observability_ok": True,
                "ordinary_user_flow_ok": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(SmokeConfig.from_environment(), args.state_output)
    except SmokeFailure as exc:
        print(f"full-stack smoke 失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
