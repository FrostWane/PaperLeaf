"""整个 Agent Run 共享的外部学术数据源策略与调用预算。"""

from __future__ import annotations

from typing import Any

PROVIDER_BY_TOOL = {
    "search_library": "library",
    "search_arxiv": "arxiv",
    "find_related_papers": "arxiv",
    "mcp__academic__search_openalex": "openalex",
    "mcp__academic__search_semantic_scholar": "semantic_scholar",
}
TOOL_BY_PROVIDER = {
    "library": "search_library",
    "arxiv": "search_arxiv",
    "openalex": "mcp__academic__search_openalex",
    "semantic_scholar": "mcp__academic__search_semantic_scholar",
}
EXTERNAL_PROVIDERS = frozenset({"arxiv", "openalex", "semantic_scholar"})
ALL_PROVIDERS = frozenset(TOOL_BY_PROVIDER)


def build_provider_run_policy(task: dict[str, Any] | None = None) -> dict[str, Any]:
    """把用户来源约束提升为可序列化、可审计的 Run 级共享状态。"""

    current = dict(task or {})
    requested_tools = {
        str(value) for value in current.get("requested_sources", []) if str(value).strip()
    }
    denied_tools = {
        str(value) for value in current.get("denied_sources", []) if str(value).strip()
    }
    requested = {
        PROVIDER_BY_TOOL[tool] for tool in requested_tools if tool in PROVIDER_BY_TOOL
    }
    denied = {PROVIDER_BY_TOOL[tool] for tool in denied_tools if tool in PROVIDER_BY_TOOL}
    if requested:
        denied.update(EXTERNAL_PROVIDERS - requested)
    requested.difference_update(denied)
    return {
        "version": 1,
        "requested": sorted(requested),
        "denied": sorted(denied),
        "attempted": {},
        "max_attempts": {provider: 1 for provider in sorted(ALL_PROVIDERS)},
        "blocked": [],
    }


def provider_for_tool(tool_name: str) -> str | None:
    return PROVIDER_BY_TOOL.get(tool_name)


def provider_can_run(policy: dict[str, Any] | None, provider: str) -> tuple[bool, str | None]:
    """只读检查来源是否还能在本 Run 中访问，不消耗预算。"""

    if provider not in ALL_PROVIDERS:
        return False, "unknown_provider"
    current = policy or {}
    denied = {str(value) for value in current.get("denied", [])}
    requested = {str(value) for value in current.get("requested", [])}
    if provider in denied or (
        provider in EXTERNAL_PROVIDERS and requested and provider not in requested
    ):
        return False, "source_excluded_by_user"
    attempted = dict(current.get("attempted", {}) or {})
    maximum = dict(current.get("max_attempts", {}) or {})
    if int(attempted.get(provider, 0) or 0) >= int(maximum.get(provider, 1) or 1):
        return False, "provider_budget_exhausted"
    return True, None


def claim_provider_attempt(
    policy: dict[str, Any], provider: str, *, tool_name: str
) -> tuple[bool, str | None]:
    """在真正访问 Provider 前原子式占用本 Run 的一次预算。"""

    allowed, reason = provider_can_run(policy, provider)
    if not allowed:
        blocked = list(policy.get("blocked", []) or [])
        blocked.append({"provider": provider, "tool": tool_name, "reason": reason})
        policy["blocked"] = blocked[-20:]
        return False, reason
    attempted = dict(policy.get("attempted", {}) or {})
    attempted[provider] = int(attempted.get(provider, 0) or 0) + 1
    policy["attempted"] = attempted
    return True, None


def release_provider_attempt(policy: dict[str, Any], provider: str) -> None:
    """Schema 在访问网络前失败时退还预算；真实空结果或失败不退还。"""

    attempted = dict(policy.get("attempted", {}) or {})
    count = int(attempted.get(provider, 0) or 0)
    if count <= 1:
        attempted.pop(provider, None)
    else:
        attempted[provider] = count - 1
    policy["attempted"] = attempted


def provider_policy_snapshot(policy: dict[str, Any] | None) -> dict[str, Any]:
    current = dict(policy or {})
    return {
        "version": int(current.get("version", 1) or 1),
        "requested": sorted({str(value) for value in current.get("requested", [])}),
        "denied": sorted({str(value) for value in current.get("denied", [])}),
        "attempted": {
            str(key): int(value)
            for key, value in sorted(dict(current.get("attempted", {}) or {}).items())
        },
        "max_attempts": {
            str(key): int(value)
            for key, value in sorted(dict(current.get("max_attempts", {}) or {}).items())
        },
        "blocked": [dict(item) for item in current.get("blocked", []) if isinstance(item, dict)],
    }
