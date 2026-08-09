import { cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PaperWorkspace } from "@/components/paper-workspace";
import { demoDataSource, resetDemoChatStateForTests } from "@/lib/data-source";

describe("PaperWorkspace 证据化产物", () => {
  beforeEach(() => {
    resetDemoChatStateForTests();
    localStorage.clear();
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("显示结构化五节，并从事实引用跳到正确物理页", async () => {
    const summarize = vi.spyOn(demoDataSource, "summarizePaper");
    const { container } = render(<PaperWorkspace demo paperId="attention" />);
    const assistant = within(container.querySelector(".workspace-desktop .workspace-assistant") as HTMLElement);
    fireEvent.click(assistant.getByRole("button", { name: "概览" }));
    fireEvent.click(assistant.getByRole("button", { name: "生成概览" }));

    expect(await assistant.findByRole("region", { name: "研究问题" })).toBeInTheDocument();
    expect(summarize).toHaveBeenNthCalledWith(1, "attention", { refresh: false });
    expect(assistant.getAllByRole("region")).toHaveLength(5);
    fireEvent.click(assistant.getByRole("button", { name: /引用 \[\d+\]，查看 PDF 第 12 页/ }));
    expect(within(container.querySelector(".workspace-desktop .workspace-reader") as HTMLElement).getByText("12 / 15")).toBeInTheDocument();

    fireEvent.click(assistant.getByRole("button", { name: "重新生成" }));
    await waitFor(() => expect(summarize).toHaveBeenNthCalledWith(2, "attention", { refresh: true }));
  });

  it("首次构建结构使用缓存策略，已有结果重新构建时明确刷新", async () => {
    const buildStructure = vi.spyOn(demoDataSource, "buildStructureGraph");
    const { container } = render(<PaperWorkspace demo paperId="attention" />);
    const assistant = within(container.querySelector(".workspace-desktop .workspace-assistant") as HTMLElement);
    fireEvent.click(assistant.getByRole("button", { name: "结构" }));
    fireEvent.click(assistant.getByRole("button", { name: "构建脑图" }));

    expect(await assistant.findByRole("list", { name: "结构节点与原文页码" })).toBeInTheDocument();
    expect(buildStructure).toHaveBeenNthCalledWith(1, "attention", { refresh: false });
    fireEvent.click(assistant.getByRole("button", { name: "重新构建" }));
    await waitFor(() => expect(buildStructure).toHaveBeenNthCalledWith(2, "attention", { refresh: true }));
  }, 45_000);

  it("未配置模型时明确说明原因并提供稍后重试，不展示伪结果", async () => {
    vi.spyOn(demoDataSource, "summarizePaper").mockRejectedValue(new Error("model_not_configured"));
    const { container } = render(<PaperWorkspace demo paperId="attention" />);
    const assistant = within(container.querySelector(".workspace-desktop .workspace-assistant") as HTMLElement);
    fireEvent.click(assistant.getByRole("button", { name: "概览" }));
    fireEvent.click(assistant.getByRole("button", { name: "生成概览" }));

    await waitFor(() => expect(assistant.getByRole("alert")).toHaveTextContent("尚未配置论文分析模型"));
    expect(assistant.getByRole("button", { name: "稍后重试" })).toBeInTheDocument();
    expect(assistant.queryByText("模型归纳")).not.toBeInTheDocument();
  });

  it("后台概括完成后自动恢复中文五节，不展示英文证据摘录", async () => {
    const ready = await demoDataSource.summarizePaper("attention", { refresh: false });
    let finishPolling: (value: typeof ready) => void = () => undefined;
    const pendingResult = new Promise<typeof ready>((resolve) => { finishPolling = resolve; });
    const summarize = vi.spyOn(demoDataSource, "summarizePaper")
      .mockResolvedValueOnce({
        ...ready,
        status: "processing",
        sections: [],
        citations: [],
        content: "Raw English abstract must never be rendered.",
      })
      .mockImplementationOnce(() => pendingResult)
      .mockResolvedValue(ready);
    const { container } = render(<PaperWorkspace demo paperId="attention" />);
    const assistant = within(container.querySelector(".workspace-desktop .workspace-assistant") as HTMLElement);

    fireEvent.click(assistant.getByRole("button", { name: "概览" }));
    fireEvent.click(assistant.getByRole("button", { name: "生成概览" }));

    expect(await assistant.findByText("概览正在后台生成")).toBeInTheDocument();
    expect(assistant.queryByText(/Raw English abstract/)).not.toBeInTheDocument();
    finishPolling(ready);
    expect(await assistant.findByRole("region", { name: "研究问题" }, { timeout: 5_000 })).toBeInTheDocument();
    expect(summarize).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem("paperleaf:artifact:attention:summary")).toBeNull();
  }, 8_000);
});
