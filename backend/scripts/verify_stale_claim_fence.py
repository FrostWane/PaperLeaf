"""在真实 PostgreSQL 上验证旧 Worker 的 claim token 无法写事件。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paperleaf_api.config import settings
from paperleaf_api.repository import SQLAlchemyRepository


async def _verify(run_id: str, stale_claim_token: str) -> None:
    repository = SQLAlchemyRepository(settings.session_secret)
    result = await repository.append_agent_run_event(
        run_id,
        "node_finished",
        {"node": "stale_worker_probe", "status": "should_not_persist"},
        event_key="recovery:stale-claim-probe",
        claim_token=stale_claim_token,
    )
    if result is not None:
        raise SystemExit("旧 claim token 意外写入成功")
    print("stale_claim_fenced=true")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stale-claim-token", required=True)
    args = parser.parse_args()
    asyncio.run(_verify(args.run_id, args.stale_claim_token))


if __name__ == "__main__":
    main()
