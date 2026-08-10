"""对既有会话执行一次真实的多轮联网论文发现回归。"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from .evaluation_harness_live import LiveHarness, LiveScenario


async def run(session_id: str, *, base_url: str, timeout_seconds: float) -> dict:
    harness = LiveHarness(base_url=base_url, timeout_seconds=timeout_seconds, concurrency=1)
    try:
        await harness.login()
        scenario = LiveScenario(
            index=1,
            category="function_mcp_followup",
            title="[实测][多轮联网] 2026 年追问",
            session_type="collection",
            question="有没有更近的论文，如2026年的",
            expected_skills=("find_related_papers",),
            web_enabled=True,
            require_citations=False,
            require_native_tools=True,
            expected_tools=("mcp__academic__search_openalex",),
        )
        result = await harness.run_scenario(scenario, session_id=session_id)
        return asdict(result)
    finally:
        await harness.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperLeaf 多轮联网发现真实回归")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--base-url", default="http://api:8000")
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args()
    result = asyncio.run(
        run(
            args.session_id,
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("structural_pass"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
