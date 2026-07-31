import { CheckCircle2, FileCheck2, ShieldCheck, ShieldX } from "lucide-react";
import type { AgentEvidenceQuality } from "@/lib/types";

export function EvidenceQualityStrip({ quality }: { quality?: AgentEvidenceQuality }) {
  if (!quality) return null;
  const claims = quality.claimCount ?? 0;
  const cited = quality.citedClaimCount ?? 0;
  const support = quality.answerSupportGrade;
  const SupportIcon = support === "supported" ? ShieldCheck : ShieldX;
  const supportLabel = support === "supported"
    ? "逐条核验通过"
    : support === "unsupported"
      ? "未通过，已拦截"
      : quality.grade === "insufficient"
        ? "证据不足，已拒答"
        : "等待答案核验";

  return (
    <div className="evidence-quality-strip" data-grade={quality.grade} aria-label="回答证据状态">
      <span><FileCheck2 size={13} />证据页 {quality.pageCount}</span>
      <span><CheckCircle2 size={13} />主张引用 {claims ? `${cited}/${claims}` : "—"}</span>
      <span><SupportIcon size={13} />{supportLabel}</span>
    </div>
  );
}
