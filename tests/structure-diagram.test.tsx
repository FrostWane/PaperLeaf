import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StructureDiagram } from "@/components/structure-diagram";
import { paperStructureGraph } from "@/lib/fixtures";

vi.mock("mermaid", () => ({ default: { initialize: vi.fn(), render: vi.fn().mockResolvedValue({ svg: "<svg></svg>" }) } }));

describe("StructureDiagram", () => {
  afterEach(cleanup);

  it("显示 5-12 个语义节点，并允许打开节点的任一物理页引用", () => {
    const onOpenPage = vi.fn();
    render(<StructureDiagram graph={paperStructureGraph} paperTitle="Attention Is All You Need" onOpenPage={onOpenPage} />);

    expect(screen.getAllByRole("listitem")).toHaveLength(8);
    fireEvent.click(screen.getByRole("button", { name: /结果：翻译质量与训练效率同步改善，查看首条证据 PDF 第 11 页/ }));
    fireEvent.click(screen.getByRole("button", { name: /引用 \[\d+\]，查看 PDF 第 12 页/ }));
    expect(onOpenPage).toHaveBeenNthCalledWith(1, 11);
    expect(onOpenPage).toHaveBeenNthCalledWith(2, 12);
    expect(screen.queryByText(/p11:c0/)).not.toBeInTheDocument();
  });
});
