import { cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PaperWorkspace } from "@/components/paper-workspace";
import { demoDataSource, resetDemoChatStateForTests } from "@/lib/data-source";

describe("PaperWorkspace 统一持久化问答", () => {
  beforeEach(() => {
    resetDemoChatStateForTests();
    localStorage.clear();
    vi.stubGlobal("crypto", { subtle: { digest: vi.fn(async () => new Uint8Array(32).buffer) } });
    vi.stubGlobal("ResizeObserver", class {
      observe() { /* 测试不需要尺寸通知。 */ }
      unobserve() { /* 测试不需要尺寸通知。 */ }
      disconnect() { /* 测试不需要尺寸通知。 */ }
    });
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("单篇助手恢复绑定当前论文的历史会话，并且只挂载一个事件订阅实例", async () => {
    const listSessions = vi.spyOn(demoDataSource, "listChatSessions");
    const subscribe = vi.spyOn(demoDataSource, "subscribeAgentRun");
    const { container } = render(<PaperWorkspace demo paperId="attention" />);
    const desktopAssistant = within(container.querySelector(".workspace-desktop .workspace-assistant") as HTMLElement);
    expect(await desktopAssistant.findByText("Transformer 的核心贡献")).toBeInTheDocument();
    expect(await desktopAssistant.findByText("这篇论文解决了什么问题？")).toBeInTheDocument();
    expect(await desktopAssistant.findByRole("heading", { name: "研究问题" })).toBeInTheDocument();
    expect(listSessions).toHaveBeenCalled();
    expect(subscribe).not.toHaveBeenCalled();
    expect(container.querySelector(".workspace-mobile .chat-workspace")).toBeNull();
  });

  it("新问题通过统一会话接口提交并显示用户消息", async () => {
    const submit = vi.spyOn(demoDataSource, "submitChatMessage");
    const { container } = render(<PaperWorkspace demo paperId="attention" />);
    const assistant = within(container.querySelector(".workspace-desktop .workspace-assistant") as HTMLElement);
    fireEvent.click(await assistant.findByRole("button", { name: /新对话/ }));
    const input = await assistant.findByPlaceholderText(/输入问题/);
    fireEvent.change(input, { target: { value: "当前论文的核心结论是什么？" } });
    fireEvent.click(assistant.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    expect(await assistant.findByText("当前论文的核心结论是什么？", { selector: ".chat-message.user > p" })).toBeInTheDocument();
  });

  it("问题被后台成功受理后消费本次选文并提交其页码与哈希", async () => {
    const submit = vi.spyOn(demoDataSource, "submitChatMessage");
    const { container } = render(<PaperWorkspace demo paperId="attention" initialPage={2} />);
    const desktop = within(container.querySelector(".workspace-desktop") as HTMLElement);
    const original = desktop.getByLabelText("原始 PDF，第 2 页");
    vi.spyOn(window, "getSelection").mockReturnValue({
      anchorNode: original,
      focusNode: original,
      toString: () => "  蛋白质   序列编码段落  ",
    } as unknown as Selection);
    fireEvent.mouseUp(original);
    expect(await desktop.findByText("已选原文 10 字", { selector: ".reader-selection-status" })).toBeVisible();

    const assistant = within(container.querySelector(".workspace-desktop .workspace-assistant") as HTMLElement);
    const input = await assistant.findByPlaceholderText(/输入问题/);
    fireEvent.change(input, { target: { value: "解释这段选文的研究作用" } });
    fireEvent.click(assistant.getByRole("button", { name: "发送问题" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith(
      expect.any(String),
      "解释这段选文的研究作用",
      expect.any(String),
      expect.objectContaining({
        clientContext: expect.objectContaining({
          paperId: "attention",
          physicalPage: 2,
          selectedText: "蛋白质 序列编码段落",
          selectedTextHash: "0".repeat(64),
        }),
      }),
    ));
    await waitFor(() => expect(desktop.queryByText(/已选原文 10 字/)).not.toBeInTheDocument());
    expect(assistant.queryByRole("button", { name: "已选原文" })).not.toBeInTheDocument();
  });

  it("问题提交失败时保留选文供用户重试", async () => {
    vi.spyOn(demoDataSource, "submitChatMessage").mockRejectedValue(new Error("问答服务暂时不可用"));
    const { container } = render(<PaperWorkspace demo paperId="attention" initialPage={2} />);
    const desktop = within(container.querySelector(".workspace-desktop") as HTMLElement);
    const original = desktop.getByLabelText("原始 PDF，第 2 页");
    vi.spyOn(window, "getSelection").mockReturnValue({
      anchorNode: original,
      focusNode: original,
      toString: () => "蛋白质 序列编码段落",
    } as unknown as Selection);
    fireEvent.mouseUp(original);
    expect(await desktop.findByText("已选原文 10 字", { selector: ".reader-selection-status" })).toBeVisible();

    const assistant = within(container.querySelector(".workspace-desktop .workspace-assistant") as HTMLElement);
    const input = await assistant.findByPlaceholderText(/输入问题/);
    fireEvent.change(input, { target: { value: "失败后仍要保留这段选文" } });
    fireEvent.click(assistant.getByRole("button", { name: "发送问题" }));

    expect(await assistant.findByText("问答服务暂时不可用")).toBeVisible();
    expect(desktop.getByText("已选原文 10 字", { selector: ".reader-selection-status" })).toBeVisible();
    expect(assistant.getByRole("button", { name: "已选原文" })).toBeVisible();
  });
});
