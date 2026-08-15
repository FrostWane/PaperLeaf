"""仅供隔离 Compose smoke 使用的确定性 OpenAI-compatible Stub。

不记录请求头、提示词或正文，也不能用于质量评测。它只证明真实 HTTP 模型协议、
Worker、LangGraph、SSE 和引用持久化链路能够连通。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="PaperLeaf deterministic model stub", version="0.9.0")


def _content(payload: dict[str, Any]) -> str:
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    system = "\n".join(
        str(item.get("content", ""))
        for item in messages
        if isinstance(item, dict) and item.get("role") == "system"
    )
    human = "\n".join(
        str(item.get("content", ""))
        for item in messages
        if isinstance(item, dict) and item.get("role") in {"user", "human"}
    )
    if "答案支持分类器" in system:
        indices = sorted({int(item) for item in re.findall(r"\[claim:(\d+)\]", human)})
        return json.dumps(
            {
                "supported": True,
                "confidence": 0.99,
                "reason_code": "answer_supported",
                "supported_claim_indices": indices,
                "unsupported_claim_indices": [],
            },
            ensure_ascii=False,
        )
    if "可回答性分类器" in system:
        return json.dumps(
            {"answerable": True, "confidence": 0.99, "reason_code": "direct_answer"},
            ensure_ascii=False,
        )
    if "查询" in system and "改写" in system:
        return json.dumps({"queries": []}, ensure_ascii=False)
    alias = re.search(r"\[chunk:(E\d+)", human)
    citation = alias.group(1) if alias else "E1"
    return (
        "这是一份用于验证 PaperLeaf 上传、物理页解析、检索和后台问答链路的合成文献。"
        f"文献正文包含可复核的 smoke 标记。[chunk:{citation}]"
    )


def _chunk(model: str, content: str, *, finish_reason: str | None = None) -> str:
    payload = {
        "id": "paperleaf-smoke",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content} if content else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    payload = await request.json()
    content = _content(payload)
    model = str(payload.get("model") or "paperleaf-smoke")
    if payload.get("stream") is True:

        async def stream() -> AsyncIterator[str]:
            step = 18
            for offset in range(0, len(content), step):
                yield _chunk(model, content[offset : offset + step])
            yield _chunk(model, "", finish_reason="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")
    return {
        "id": "paperleaf-smoke",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
