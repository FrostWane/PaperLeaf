import { act, cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PaperWorkspace } from "@/components/paper-workspace";
import { demoDataSource } from "@/lib/data-source";
import type { AgentAnswer, AgentAskStreamHandlers } from "@/lib/types";

describe("PaperWorkspace 问答增量状态", () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("立即显示问题，增量加入回答和引文，并在失败后保留现场", async () => {
    vi.stubGlobal("ResizeObserver", class {
      observe() { /* 测试中不需要真实尺寸通知。 */ }
      unobserve() { /* 测试中不需要真实尺寸通知。 */ }
      disconnect() { /* 测试中不需要真实尺寸通知。 */ }
    });
    let handlers: AgentAskStreamHandlers | undefined;
    let rejectAsk: ((reason: unknown) => void) | undefined;
    const ask = vi.spyOn(demoDataSource, "ask").mockImplementation((question, paperIds, nextHandlers) => {
      handlers = nextHandlers;
      expect(question).toBe("当前论文的核心结论是什么？");
      expect(paperIds).toEqual(["attention"]);
      return new Promise<AgentAnswer>((_resolve, reject) => { rejectAsk = reject; });
    });

    const { container } = render(<PaperWorkspace demo paperId="attention" />);
    // 回归桌面端和移动端同时挂载时，同名输入框被重复注册、桌面输入被隐藏表单覆盖的问题。
    const assistant = within(container.querySelector(".workspace-desktop") as HTMLElement);
    const questionInput = assistant.getByPlaceholderText("继续追问这篇论文…");
    fireEvent.change(questionInput, { target: { value: "当前论文的核心结论是什么？" } });
    fireEvent.submit(questionInput.closest("form") as HTMLFormElement);

    await waitFor(() => expect(ask).toHaveBeenCalledOnce());
    expect(assistant.getByText("当前论文的核心结论是什么？", { selector: ".question-text" })).toBeInTheDocument();
    expect(assistant.getByText("正在准备基于文献证据的回答…")).toBeInTheDocument();

    act(() => handlers?.onAnswerUpdate?.("已收到一部分证据回答"));
    expect(assistant.getByText(/已收到一部分证据回答/)).toBeInTheDocument();

    act(() => handlers?.onCitationsUpdate?.([{
      id: "attention:p2:c0",
      chunkId: "attention:p2:c0",
      paperId: "attention",
      paperTitle: "Attention Is All You Need",
      page: 2,
      quote: "原文证据",
      href: "/api/v1/papers/attention/file#page=2",
    }]));
    expect(assistant.getByRole("button", { name: "引用 [1]，查看 PDF 第 2 页" })).toBeInTheDocument();

    await act(async () => rejectAsk?.(new Error("问答运行失败")));
    expect(assistant.getByRole("alert")).toHaveTextContent("问答运行失败");
    expect(assistant.getByText("当前论文的核心结论是什么？", { selector: ".question-text" })).toBeInTheDocument();
    expect(assistant.getByText(/已收到一部分证据回答/)).toBeInTheDocument();
  });
});
