import type { ArtifactCitation, PaperArtifactFallbackReason, PaperArtifactStatus, StructureNodeType, SummarySectionKey } from "./types";

export const summarySectionTitles: Record<SummarySectionKey, string> = {
  research_problem: "研究问题",
  core_method: "核心方法",
  experiment_setup: "实验设置",
  main_results: "主要结果",
  limitations: "局限与适用范围",
};

export const structureNodeTypeLabels: Record<StructureNodeType, string> = {
  research_problem: "研究问题",
  background: "背景",
  method: "方法",
  data: "数据",
  experiment: "实验",
  result: "结果",
  limitation: "局限",
};

export const structureNodeTypes = new Set<StructureNodeType>(Object.keys(structureNodeTypeLabels) as StructureNodeType[]);
export const summarySectionKeys = new Set<SummarySectionKey>(Object.keys(summarySectionTitles) as SummarySectionKey[]);

export function artifactFailureMessage(reason?: PaperArtifactFallbackReason): string {
  if (!reason) return "AI 产物生成失败，尚未保存任何未经核验的内容。";
  const messages: Record<string, string> = {
    model_not_configured: "尚未配置论文分析模型，当前无法生成可信的 AI 产物。",
    model_timeout: "论文分析模型响应超时，本次没有保存不完整结果。",
    citation_validation_failed: "模型给出的页码引用未通过证据核验，本次结果已被拦截。",
    invalid_output: "模型输出不符合结构化格式要求，本次结果已被拦截。",
  };
  return messages[reason] ?? (/^[a-z0-9_.-]+$/i.test(reason) ? "AI 产物生成失败，尚未保存任何未经核验的内容。" : reason);
}

export function normalizeArtifactStatus(value: unknown, stale: boolean): PaperArtifactStatus {
  if (stale) return "stale";
  return value === "failed" || value === "processing" || value === "stale" ? value : "ready";
}

export function uniqueArtifactCitations(citations: ArtifactCitation[]): ArtifactCitation[] {
  const seen = new Set<string>();
  return citations.filter((citation) => {
    const key = `${citation.chunkId}:${citation.physicalPage}`;
    if (!citation.chunkId || citation.physicalPage < 1 || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
