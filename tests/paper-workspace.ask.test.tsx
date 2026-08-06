import { cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PaperWorkspace } from "@/components/paper-workspace";
import { demoDataSource, resetDemoChatStateForTests } from "@/lib/data-source";

describe("PaperWorkspace 统一持久化问答", () => {
  beforeEach(() => {
    resetDemoChatStateForTests();
    localStorage.clear();
    vi.stubGlobal("ResizeObserver", class {
      observe() { /* 测试不需要尺寸通知。 */ }
      unobserve() { /* 测试不需要尺寸通知。 */ }
      disconnect() { /* 测试不需要尺寸通知。 */ }
    });
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

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

  it("新问题通过统一会话接口提交，运行中禁用重复发送并提供取消", async () => {
    const submit = vi.spyOn(demoDataSource, "submitChatMessage");
    const { container } = render(<PaperWorkspace demo paperId="attention" />);
    const assistant = within(container.querySelector(".workspace-desktop .workspace-assistant") as HTMLElement);
    fireEvent.click(await assistant.findByRole("button", { name: /新对话/ }));
    const input = await assistant.findByPlaceholderText(/输入问题/);
    fireEvent.change(input, { target: { value: "当前论文的核心结论是什么？" } });
    fireEvent.click(assistant.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    expect(await assistant.findByText("当前论文的核心结论是什么？", { selector: ".chat-message.user > p" })).toBeInTheDocument();
    expect(assistant.getByRole("button", { name: "发送问题" })).toBeDisabled();
    expect(await assistant.findByRole("button", { name: "取消运行" })).toBeInTheDocument();
  });
});
