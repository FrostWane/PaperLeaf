import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StructuredSummary, SummaryContent } from "@/components/summary-content";
import type { ArtifactCitation } from "@/lib/types";

const citations: ArtifactCitation[] = [
  { chunkId: "paper-1:p4:c0", physicalPage: 4 },
  { chunkId: "paper-1:p7:c2", physicalPage: 7 },
];

describe("SummaryContent", () => {
  afterEach(cleanup);

  it("将 Markdown 标题、列表和段落渲染为语义化元素", () => {
    render(<SummaryContent content={`# 研究概览

第一行摘要
继续说明。

## 关键发现
- 提升检索质量
- 降低无依据回答

### 实验步骤
1. 建立索引
2. 验证证据`} citations={[]} onOpenPage={vi.fn()} />);

    expect(screen.getByRole("heading", { level: 4, name: "研究概览" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 5, name: "关键发现" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 6, name: "实验步骤" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
    expect(screen.getByText("第一行摘要 继续说明。").tagName).toBe("P");

    const lists = screen.getAllByRole("list");
    expect(lists[0].tagName).toBe("UL");
    expect(within(lists[0]).getAllByRole("listitem")).toHaveLength(2);
    expect(lists[1].tagName).toBe("OL");
    expect(within(lists[1]).getAllByRole("listitem")).toHaveLength(2);
  });

  it("将 chunk 与物理页标记映射为可回读的内联引用", () => {
    const onOpenPage = vi.fn();
    render(<SummaryContent
      content="方法由原文支持 [chunk:paper-1:p4:c0]，限制见 [物理页 7]。"
      citations={citations}
      onOpenPage={onOpenPage}
    />);

    const chunkReference = screen.getByRole("button", { name: "引用 [1]，查看 PDF 第 4 页" });
    const pageReference = screen.getByRole("button", { name: "引用 [2]，查看 PDF 第 7 页" });
    expect(chunkReference).toHaveTextContent("[1]");
    expect(pageReference).toHaveTextContent("[2]");

    fireEvent.click(chunkReference);
    fireEvent.click(pageReference);
    expect(onOpenPage).toHaveBeenNthCalledWith(1, 4);
    expect(onOpenPage).toHaveBeenNthCalledWith(2, 7);
  });

  it("保留无法映射的引用标记，避免丢失原始信息", () => {
    render(<SummaryContent
      content="尚未收录 [chunk:missing]，页码也未知 [物理页 99]。"
      citations={citations}
      onOpenPage={vi.fn()}
    />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText("尚未收录 [chunk:missing]，页码也未知 [物理页 99]。")).toBeInTheDocument();
  });

  it("结构化五节中的每条事实保留独立的多页引用", () => {
    const onOpenPage = vi.fn();
    render(<StructuredSummary sections={[
      { key: "research_problem", title: "研究问题", facts: [{ text: "循环计算限制并行。", citations }] },
      { key: "core_method", title: "核心方法", facts: [{ text: "以注意力替代循环。", citations: [citations[0]] }] },
      { key: "experiment_setup", title: "实验设置", facts: [{ text: "比较翻译任务。", citations: [citations[1]] }] },
      { key: "main_results", title: "主要结果", facts: [{ text: "训练更快。", citations: [citations[1]] }] },
      { key: "limitations", title: "局限与适用范围", facts: [{ text: "长序列成本较高。", citations: [citations[0]] }] },
    ]} onOpenPage={onOpenPage} />);

    expect(screen.getAllByRole("region")).toHaveLength(5);
    fireEvent.click(screen.getByRole("button", { name: "查看 研究问题第 1 条事实的引用 2，PDF 第 7 页" }));
    expect(onOpenPage).toHaveBeenCalledWith(7);
  });
});
