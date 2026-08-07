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
  const generic = "这次概括没有生成成功。论文原文和索引都已保留，你可以稍后重试，或先在“问答”中询问一个更具体的问题。";
  if (!reason) return generic;
  const messages: Record<string, string> = {
    model_not_configured: "尚未配置论文分析模型。论文原文和索引不受影响，配置模型后即可重新生成中文概括。",
    model_timeout: "论文分析模型响应超时。后台没有保存半成品，你可以稍后重试，或先在“问答”中询问一个更具体的问题。",
    citation_validation_failed: "模型给出的页码引用未通过证据核验，因此没有展示可能误导你的概括。你可以稍后重试。",
    invalid_output: "模型输出不符合结构化格式要求，因此没有展示这次结果。论文原文和索引都已保留，你可以稍后重试。",
  };
  if (messages[reason]) return messages[reason];
  // 后端详情必须是中文用户提示；即使上游意外返回英文异常，也不能把它直出到界面。
  return /[\u3400-\u9fff]/.test(reason) ? reason : generic;
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
