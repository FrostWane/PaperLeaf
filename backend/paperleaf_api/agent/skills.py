"""仓库内版本化 Skill Manifest 注册表。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

KNOWN_TOOLS = {
    "search_current_paper",
    "search_library",
    "get_page_text",
    "search_arxiv",
    "get_crossref_metadata",
    "find_related_papers",
    "request_import",
    "summarize_paper",
    "build_structure_graph",
}
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    version: int = Field(ge=1)
    description: str = Field(min_length=8, max_length=240)
    allowed_tools: list[str] = Field(default_factory=list, max_length=12)
    max_tool_steps: int = Field(default=3, ge=0, le=4)
    requires_evidence: bool = True
    web_policy: Literal["disabled", "local_first", "explicit_only"] = "disabled"
    approval_policy: Literal["none", "write_actions"] = "none"

    @field_validator("allowed_tools")
    @classmethod
    def validate_tools(cls, values: list[str]) -> list[str]:
        unknown = sorted(set(values) - KNOWN_TOOLS)
        if unknown:
            raise ValueError(f"Skill 声明了未知工具：{', '.join(unknown)}")
        if len(values) != len(set(values)):
            raise ValueError("Skill allowed_tools 不能重复")
        return values


@dataclass(frozen=True)
class SkillDefinition:
    manifest: SkillManifest
    instructions: str
    source: Path

    @property
    def identity(self) -> str:
        return f"{self.manifest.name}@{self.manifest.version}"


class SkillRegistryError(ValueError):
    """Manifest 不完整、重名或引用未知工具。"""


class SkillRegistry:
    def __init__(self, definitions: list[SkillDefinition]) -> None:
        self._definitions: dict[str, SkillDefinition] = {}
        identities: set[str] = set()
        for definition in definitions:
            name = definition.manifest.name
            if name in self._definitions:
                raise SkillRegistryError(f"Skill 名称重复：{name}")
            if definition.identity in identities:
                raise SkillRegistryError(f"Skill 版本重复：{definition.identity}")
            self._definitions[name] = definition
            identities.add(definition.identity)
        if not self._definitions:
            raise SkillRegistryError("Skill Registry 不能为空")

    @classmethod
    def from_directory(cls, directory: Path) -> SkillRegistry:
        definitions: list[SkillDefinition] = []
        for path in sorted(directory.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            match = _FRONTMATTER.match(text)
            if not match:
                raise SkillRegistryError(f"{path.name} 缺少合法 YAML Frontmatter")
            try:
                manifest = SkillManifest.model_validate(yaml.safe_load(match.group(1)) or {})
            except (yaml.YAMLError, ValidationError) as exc:
                raise SkillRegistryError(f"{path.name} Manifest 无效：{exc}") from exc
            instructions = match.group(2).strip()
            if len(instructions) < 20:
                raise SkillRegistryError(f"{path.name} 缺少完整 Skill 指令")
            definitions.append(SkillDefinition(manifest, instructions, path))
        return cls(definitions)

    @classmethod
    def default(cls) -> SkillRegistry:
        return cls.from_directory(Path(__file__).resolve().parents[1] / "skills")

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": item.manifest.name,
                "version": item.manifest.version,
                "description": item.manifest.description,
            }
            for item in sorted(self._definitions.values(), key=lambda value: value.manifest.name)
        ]

    def get(self, name: str) -> SkillDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise SkillRegistryError(f"未知 Skill：{name}") from exc

    def route(self, query: str, *, intent: str, scope: str, web_enabled: bool) -> SkillDefinition:
        """确定性保底路由；Function Calling 阶段可由模型从同一 Catalog 选择。"""

        # Context Engine 的受信标签不是用户意图，不能让“已验证阅读上下文”等
        # 内部说明污染 Skill 分类。
        user_query = query.split("\n\n[已验证阅读上下文]", 1)[0]
        normalized = user_query.casefold()
        if any(marker in normalized for marker in ("原文", "哪一页", "出处", "怎么写")):
            selected = "trace_original"
        elif intent == "comparison" or any(marker in normalized for marker in ("比较", "对比")):
            selected = "compare_papers"
        elif any(marker in normalized for marker in ("验证", "核实", "是否真的", "支持吗")):
            selected = "verify_claim"
        elif any(marker in normalized for marker in ("结构图", "脑图", "研究逻辑")):
            selected = "build_research_map"
        elif any(marker in normalized for marker in ("总结", "概括", "讲了什么")):
            selected = "summarize_paper"
        elif web_enabled and any(
            marker in normalized for marker in ("相关论文", "搜索论文", "最新研究")
        ):
            selected = "find_related_papers"
        else:
            selected = "paper_qa"
        definition = self.get(selected)
        if scope == "library" and selected == "trace_original":
            return self.get("paper_qa")
        return definition
