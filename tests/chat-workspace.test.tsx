import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatWorkspace, progressiveChunkSize } from "@/components/chat-workspace";
import { demoDataSource } from "@/lib/data-source";
import type { AgentRunSnapshot, ChatMessage, ChatSession } from "@/lib/types";

const createdAt = "2026-08-06T10:00:00Z";

function activeSession(): ChatSession {
  return { id: "s-active", title: "恢复测试", type: "library", currentRunId: "r-active", currentRunStatus: "running", createdAt, updatedAt: createdAt };
}

function activeRun(answer = "第一段已核验。\n\n第二段已核验。"): AgentRunSnapshot {
  return { runId: "r-active", sessionId: "s-active", status: "running", cancelRequested: false, answer, citations: [], createdAt, updatedAt: createdAt };
}

describe("ChatWorkspace", () => {
  afterEach(() => { cleanup(); localStorage.clear(); });

  it("示例问题只填入并聚焦输入框，不自动提交", async () => {
    const submit = vi.fn();
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([]),
      submitChatMessage: submit,
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    const prompt = await screen.findByRole("button", { name: /比较这些论文所采用的方法/ });
    fireEvent.click(prompt);
    const input = screen.getByPlaceholderText(/输入问题/);
    expect(input).toHaveValue("比较这些论文所采用的方法与关键假设");
    expect(input).toHaveFocus();
    expect(submit).not.toHaveBeenCalled();
  });

  it("展示可移除的阅读上下文，并只提交保留的上下文", async () => {
    const session: ChatSession = { id: "s-context", title: "新对话", type: "paper", paperId: "paper-1", createdAt, updatedAt: createdAt };
    const submit = vi.fn().mockResolvedValue({ sessionId: session.id, messageId: "m-context", runId: "r-context", status: "pending", replayed: false });
    const accepted = vi.fn();
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      updateChatSession: vi.fn().mockResolvedValue({ ...session, title: "原文为什么这样处理" }),
      submitChatMessage: submit,
      getAgentRun: vi.fn().mockResolvedValue({ ...activeRun(""), runId: "r-context", sessionId: session.id, status: "pending" as const }),
      subscribeAgentRun: vi.fn(async (_runId, _handlers, options) => { await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true })); }),
    };
    render(<ChatWorkspace
      binding={{ type: "paper", paperId: "paper-1" }}
      scopeLabel="DeepDTA"
      dataSource={source}
      clientContext={{ route: "/library/paper-1", paperId: "paper-1", physicalPage: 4, selectedText: "蛋白质序列编码段落", activePanel: "chat" }}
      onClientContextAccepted={accepted}
    />);

    const context = await screen.findByLabelText("本次提问上下文");
    expect(within(context).getByText("DeepDTA")).toBeInTheDocument();
    fireEvent.click(within(context).getByRole("button", { name: /PDF 第 4 页/ }));
    const input = screen.getByPlaceholderText(/输入问题/);
    fireEvent.change(input, { target: { value: "原文为什么这样处理" } });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(submit).toHaveBeenCalledWith(
      session.id,
      "原文为什么这样处理",
      expect.any(String),
      {
        webEnabled: false,
        clientContext: expect.objectContaining({
          paperId: "paper-1",
          paperTitle: "DeepDTA",
          physicalPage: undefined,
          selectedText: "蛋白质序列编码段落",
        }),
      },
    ));
    expect(accepted).toHaveBeenCalledWith(expect.objectContaining({
      paperId: "paper-1",
      selectedText: "蛋白质序列编码段落",
    }));
  });

  it("Enter 发送，Shift+Enter 与中文输入法选词不误发，连续 Enter 不重复提交", async () => {
    const session: ChatSession = { id: "s-enter", title: "新对话", type: "library", createdAt, updatedAt: createdAt };
    const submit = vi.fn().mockResolvedValue({ sessionId: session.id, messageId: "m-enter", runId: "r-enter", status: "pending", replayed: false });
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      updateChatSession: vi.fn().mockResolvedValue({ ...session, title: "比较实验结果" }),
      submitChatMessage: submit,
      getAgentRun: vi.fn().mockResolvedValue({ ...activeRun(""), runId: "r-enter", sessionId: session.id, status: "pending" as const }),
      subscribeAgentRun: vi.fn(async (_runId, _handlers, options) => { await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true })); }),
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    const input = await screen.findByPlaceholderText(/输入问题/);
    fireEvent.change(input, { target: { value: "比较实验结果" } });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", shiftKey: true });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", isComposing: true });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 229 });
    expect(submit).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    expect(screen.getByText("Enter 发送 · Shift + Enter 换行")).toBeInTheDocument();
  });

  it("历史回答失败时显示对应助手占位，不把连续重试伪装成重复用户消息", async () => {
    const session: ChatSession = {
      id: "s-failed",
      title: "失败回答",
      type: "paper",
      paperId: "paper-1",
      currentRunId: "r-failed",
      currentRunStatus: "failed",
      createdAt,
      updatedAt: createdAt,
    };
    const messages: ChatMessage[] = [
      { id: "m-user", sessionId: session.id, role: "user", sequence: 1, status: "completed", content: "这篇文章讲了什么", citations: [], runId: "r-failed", createdAt, updatedAt: createdAt },
      { id: "m-assistant", sessionId: session.id, role: "assistant", sequence: 2, status: "failed", content: "", citations: [], runId: "r-failed", createdAt, updatedAt: createdAt },
    ];
    const failedRun: AgentRunSnapshot = {
      runId: "r-failed",
      sessionId: session.id,
      status: "failed",
      cancelRequested: false,
      answer: "",
      citations: [],
      error: "回答仍未通过证据核验",
      createdAt,
      updatedAt: createdAt,
    };
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: vi.fn().mockResolvedValue(messages),
      getAgentRun: vi.fn().mockResolvedValue(failedRun),
    };

    render(<ChatWorkspace binding={{ type: "paper", paperId: "paper-1" }} scopeLabel="测试论文" dataSource={source} />);

    expect(await screen.findByText("本次回答未完成，请重试。")).toBeInTheDocument();
    expect(screen.getAllByText("这篇文章讲了什么")).toHaveLength(1);
  });

  it("新对话尚未创建完成时禁用输入，避免问题误发到旧会话", async () => {
    const previous: ChatSession = { id: "s-old", title: "旧对话", type: "library", createdAt, updatedAt: createdAt };
    const next: ChatSession = { id: "s-new", title: "新对话", type: "library", createdAt, updatedAt: "2026-08-07T10:00:00Z" };
    let resolveCreate!: (session: ChatSession) => void;
    const submit = vi.fn().mockResolvedValue({ sessionId: next.id, messageId: "m-new", runId: "r-new", status: "pending", replayed: false });
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValueOnce([previous]).mockResolvedValue([next, previous]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      createChatSession: vi.fn(() => new Promise<ChatSession>((resolve) => { resolveCreate = resolve; })),
      updateChatSession: vi.fn().mockResolvedValue({ ...next, title: "新会话问题" }),
      submitChatMessage: submit,
      getAgentRun: vi.fn().mockResolvedValue({ ...activeRun(""), runId: "r-new", sessionId: next.id, status: "pending" as const }),
      subscribeAgentRun: vi.fn(async (_runId, _handlers, options) => { await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true })); }),
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    await waitFor(() => expect(screen.getAllByText("旧对话")).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "新对话" }));
    const input = screen.getByRole("textbox", { name: "向文献提问" });
    expect(input).toBeDisabled();
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    expect(submit).not.toHaveBeenCalled();

    act(() => resolveCreate(next));
    await waitFor(() => expect(input).toBeEnabled());
    await waitFor(() => expect(source.listChatMessages).toHaveBeenCalledWith(next.id));
    fireEvent.change(input, { target: { value: "新会话问题" } });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(submit).toHaveBeenCalledWith(next.id, "新会话问题", expect.any(String), { webEnabled: false }));
  });

  it("跨文献历史会话每 15 条分页并按最近更新时间排序", async () => {
    const sessions: ChatSession[] = Array.from({ length: 16 }, (_, index) => ({
      id: `session-${index + 1}`,
      title: `会话 ${String(index + 1).padStart(2, "0")}`,
      type: "library",
      createdAt,
      updatedAt: `2026-08-${String(16 - index).padStart(2, "0")}T10:00:00Z`,
    }));
    const source = { ...demoDataSource, listChatSessions: vi.fn().mockResolvedValue(sessions), listChatMessages: vi.fn().mockResolvedValue([]) };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);

    const history = await screen.findByRole("complementary", { name: "历史对话" });
    expect(within(history).getByText("会话 01")).toBeInTheDocument();
    expect(within(history).queryByText("会话 16")).not.toBeInTheDocument();
    expect(within(history).getByText("第 1 / 2 页")).toBeInTheDocument();
    fireEvent.click(within(history).getByRole("button", { name: "下一页" }));
    expect(await within(history).findByText("会话 16")).toBeInTheDocument();
    expect(within(history).getByText("第 2 / 2 页")).toBeInTheDocument();
  });

  it("切换集合时忽略其他范围的本地恢复记录，并打开当前集合最近会话", async () => {
    localStorage.setItem("paperleaf:chat:library", "session-other-scope");
    const sessions: ChatSession[] = [
      { id: "session-old", title: "旧集合会话", type: "collection", collectionId: "collection-current", createdAt, updatedAt: "2026-08-10T10:00:00Z" },
      { id: "session-new", title: "最新集合会话", type: "collection", collectionId: "collection-current", createdAt, updatedAt: "2026-08-13T10:00:00Z" },
      { id: "session-other-scope", title: "其他范围会话", type: "library", createdAt, updatedAt: "2026-08-14T10:00:00Z" },
    ];
    const listMessages = vi.fn().mockResolvedValue([]);
    const source = { ...demoDataSource, listChatSessions: vi.fn().mockResolvedValue(sessions), listChatMessages: listMessages };

    render(<ChatWorkspace binding={{ type: "collection", collectionId: "collection-current" }} scopeLabel="当前集合" dataSource={source} />);

    await waitFor(() => expect(listMessages).toHaveBeenCalledWith("session-new"));
    expect(screen.getAllByText("最新集合会话").length).toBeGreaterThan(0);
    expect(screen.queryByText("其他范围会话")).toBeInTheDocument();
  });

  it("已核验段落在界面中逐字呈现，而不是整段瞬间出现", async () => {
    const session = activeSession();
    let streamHandlers: Parameters<typeof demoDataSource.subscribeAgentRun>[1] | undefined;
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      getAgentRun: vi.fn().mockResolvedValue(activeRun("")),
      subscribeAgentRun: vi.fn(async (_runId, handlers, options) => {
        streamHandlers = handlers;
        await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true }));
      }),
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    await waitFor(() => expect(streamHandlers).toBeDefined());
    const answer = "这是逐字输出的已核验回答。";
    act(() => streamHandlers?.onAnswerUpdate?.(answer));
    const live = await screen.findByLabelText("PaperLeaf 正在呈现回答");
    expect(screen.queryByText(answer)).not.toBeInTheDocument();
    await waitFor(() => {
      const partial = live.querySelector(".safe-markdown")?.textContent ?? "";
      expect(partial.length).toBeGreaterThan(0);
      expect(partial.length).toBeLessThan(answer.length);
    });
    expect(await screen.findByText(answer)).toBeInTheDocument();
  });

  it("流式积压时只使用短字符块追赶，接近结尾后恢复逐字输出", () => {
    expect(progressiveChunkSize(1600)).toBe(12);
    expect(progressiveChunkSize(800)).toBe(9);
    expect(progressiveChunkSize(300)).toBe(6);
    expect(progressiveChunkSize(120)).toBe(4);
    expect(progressiveChunkSize(50)).toBe(2);
    expect(progressiveChunkSize(20)).toBe(1);
  });

  it("紧凑展示并行跨文献比较、部分失败与安全回退，不展示内部任务内容", async () => {
    const session = activeSession();
    let streamHandlers: Parameters<typeof demoDataSource.subscribeAgentRun>[1] | undefined;
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      getAgentRun: vi.fn().mockResolvedValue(activeRun("")),
      subscribeAgentRun: vi.fn(async (_runId, handlers, options) => {
        streamHandlers = handlers;
        await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true }));
      }),
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    await waitFor(() => expect(streamHandlers).toBeDefined());

    act(() => {
      streamHandlers?.onActivity?.({ key: "comparison:plan", node: "plan_comparison", label: "拆分跨文献比较", step: 9, status: "completed", kind: "comparison_plan", total: 2 });
      streamHandlers?.onActivity?.({ key: "subtask:s1", node: "compare_subtask", label: "并行整理论文证据", step: 11, status: "completed", kind: "comparison_subtask", subtaskId: "s1", ordinal: 1, total: 2, findingCount: 4 });
      streamHandlers?.onActivity?.({ key: "subtask:s2", node: "compare_subtask", label: "并行整理论文证据", step: 12, status: "failed", kind: "comparison_subtask", rawStatus: "timeout", subtaskId: "s2", ordinal: 2, total: 2 });
    });

    const status = await screen.findByRole("region", { name: "并行跨文献比较状态" });
    expect(within(status).getByText("已分为 2 组分析")).toBeInTheDocument();
    expect(within(status).getByText("第 1/2 组已整理 4 条候选证据")).toBeInTheDocument();
    expect(within(status).getByText("第 2/2 组分析超时，将基于其余证据继续")).toBeInTheDocument();
    expect(status).toHaveTextContent("分析进度");
    expect(status).not.toHaveTextContent("chunk");

    act(() => streamHandlers?.onActivity?.({ key: "comparison:merge", node: "merge_comparison", label: "合并并去重证据", step: 14, status: "completed", kind: "comparison_merge", partialFailure: true }));
    expect(await within(status).findByText("部分分析未完成，回答仅使用已完成并核验的证据")).toBeInTheDocument();
    expect(within(status).queryByText("第 1/2 组已整理 4 条候选证据")).not.toBeInTheDocument();
    expect(status).toHaveTextContent("证据已整理");

    act(() => streamHandlers?.onActivity?.({ key: "comparison:merge", node: "merge_comparison", label: "合并并去重证据", step: 14, status: "completed", kind: "comparison_merge", fallbackToV1: true, fallbackReason: "all_subtasks_failed" }));
    expect(await within(status).findByText("并行分析均未完成，已切换为标准检索")).toBeInTheDocument();
  });

  it("恢复跨文献比较运行时先给出稳定占位，不暴露编排版本", async () => {
    const session = activeSession();
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      getAgentRun: vi.fn().mockResolvedValue({ ...activeRun(""), orchestrationVersion: "specialist_subgraph_v3" }),
      subscribeAgentRun: vi.fn(async (_runId, _handlers, options) => {
        await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true }));
      }),
    };

    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);

    const status = await screen.findByRole("region", { name: "并行跨文献比较状态" });
    expect(within(status).getByText("正在恢复比较进度…")).toBeInTheDocument();
    expect(status).not.toHaveTextContent("specialist_subgraph_v3");
  });

  it("重新打开已完成的跨文献会话时补放并行轨迹", async () => {
    const session: ChatSession = {
      ...activeSession(),
      currentRunStatus: "completed",
    };
    const completedRun: AgentRunSnapshot = {
      ...activeRun("比较结论。"),
      status: "completed",
      orchestrationVersion: "compare_map_reduce_v2",
    };
    const subscribe = vi.fn(async (_runId, handlers) => {
      handlers.onActivity?.({
        key: "comparison:merge",
        node: "merge_comparison",
        label: "合并并去重证据",
        step: 14,
        status: "completed",
        kind: "comparison_merge",
      });
      handlers.onRunUpdate?.(completedRun);
    });
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn()
        .mockResolvedValueOnce([session])
        .mockResolvedValue([{ ...session, updatedAt: "2026-08-15T10:00:00.000Z" }]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      getAgentRun: vi.fn().mockResolvedValue(completedRun),
      subscribeAgentRun: subscribe,
    };

    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);

    const status = await screen.findByRole("region", { name: "并行跨文献比较状态" });
    expect(within(status).getByText("证据合并完成")).toBeInTheDocument();
    await waitFor(() => expect(source.listChatSessions).toHaveBeenCalledTimes(2));
    expect(within(status).getByText("证据合并完成")).toBeInTheDocument();
    expect(subscribe).toHaveBeenCalledTimes(1);
  });

  it("普通单篇问答不展示并行比较状态", async () => {
    const session = activeSession();
    let streamHandlers: Parameters<typeof demoDataSource.subscribeAgentRun>[1] | undefined;
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      getAgentRun: vi.fn().mockResolvedValue(activeRun("")),
      subscribeAgentRun: vi.fn(async (_runId, handlers, options) => {
        streamHandlers = handlers;
        await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true }));
      }),
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    await waitFor(() => expect(streamHandlers).toBeDefined());
    act(() => streamHandlers?.onActivity?.({ key: "1:retrieve_library", node: "retrieve_library", label: "检索文献证据", step: 1, status: "running", kind: "node" }));
    expect(screen.queryByRole("region", { name: "并行跨文献比较状态" })).not.toBeInTheDocument();
  });

  it("用户消息和 Agent 回复使用明确方向，并统一展示可回读引用", async () => {
    const session: ChatSession = { id: "s-chat", title: "对齐测试", type: "library", createdAt, updatedAt: createdAt };
    const citation = { id: "citation-1", paperId: "p1", paperTitle: "Attention Is All You Need", page: 4, chunkId: "p1:p4:c1", quote: "The encoder maps an input sequence to representations.", href: "/library/p1?page=4" };
    const chatMessages: ChatMessage[] = [
      { id: "m-user", sessionId: session.id, role: "user", sequence: 1, status: "completed", content: "核心方法是什么？", citations: [], createdAt, updatedAt: createdAt },
      { id: "m-assistant", sessionId: session.id, role: "assistant", sequence: 2, status: "completed", content: "核心方法是自注意力。", citations: [citation], createdAt, updatedAt: createdAt },
    ];
    const source = { ...demoDataSource, listChatSessions: vi.fn().mockResolvedValue([session]), listChatMessages: vi.fn().mockResolvedValue(chatMessages) };

    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);

    const userMessage = await screen.findByLabelText("你的消息");
    const assistantMessage = screen.getByLabelText("PaperLeaf 回复");
    expect(userMessage).toHaveClass("user");
    expect(assistantMessage).toHaveClass("assistant");
    const citations = within(assistantMessage).getByLabelText("引用来源，共 1 条");
    expect(within(citations).getByText("引用来源 · 1")).toBeInTheDocument();
    expect(within(citations).getByRole("button", { name: /Attention Is All You Need/ })).toHaveAttribute("title", "打开《Attention Is All You Need》PDF 第 4 页");
  });

  it("活跃 Run 恢复时合并为一个权威回答，重挂载与补发不会重复段落", async () => {
    const session = activeSession();
    const partialMessages: ChatMessage[] = [
      { id: "m-user", sessionId: session.id, role: "user", sequence: 1, status: "completed", content: "问题", citations: [], runId: "r-active", createdAt, updatedAt: createdAt },
      { id: "m-partial", sessionId: session.id, role: "assistant", sequence: 2, status: "streaming", content: "第一段已核验。", citations: [], runId: "r-active", createdAt, updatedAt: createdAt },
    ];
    const subscribe = vi.fn(async (_runId, handlers, options) => {
      handlers.onAnswerUpdate?.("第一段已核验。\n\n第二段已核验。");
      await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true }));
    });
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: vi.fn().mockResolvedValue(partialMessages),
      getAgentRun: vi.fn().mockResolvedValue(activeRun()),
      subscribeAgentRun: subscribe,
    };

    const first = render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    await screen.findByText("第二段已核验。");
    expect(screen.getAllByText("第一段已核验。")).toHaveLength(1);
    expect(screen.getAllByText("第二段已核验。")).toHaveLength(1);
    first.unmount();

    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    await screen.findByText("第二段已核验。");
    expect(screen.getAllByText("第一段已核验。")).toHaveLength(1);
    expect(screen.getAllByText("第二段已核验。")).toHaveLength(1);
    await waitFor(() => expect(subscribe).toHaveBeenCalledTimes(2));
  });

  it("同一次用户动作在网络不确定后重试会复用幂等键", async () => {
    const session: ChatSession = { id: "s1", title: "新对话", type: "library", createdAt, updatedAt: createdAt };
    const keys: string[] = [];
    const submit = vi.fn(async (_sessionId: string, _content: string, key: string) => {
      keys.push(key);
      if (keys.length === 1) throw new TypeError("网络连接失败");
      return { sessionId: "s1", messageId: "m1", runId: "r1", status: "pending" as const, replayed: true };
    });
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      updateChatSession: vi.fn().mockResolvedValue({ ...session, title: "比较两种方法" }),
      submitChatMessage: submit,
      getAgentRun: vi.fn().mockResolvedValue({ ...activeRun(""), runId: "r1", sessionId: "s1", status: "pending" as const }),
      subscribeAgentRun: vi.fn(async (_runId, _handlers, options) => {
        await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true }));
      }),
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    const input = await screen.findByPlaceholderText(/输入问题/);
    fireEvent.change(input, { target: { value: "比较两种方法" } });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("网络连接失败");
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    expect(keys[0]).toBe(keys[1]);
  });

  it("interrupted 运行阻止重复提交但不会形成 SSE 永久重连", async () => {
    const session = { ...activeSession(), currentRunStatus: "interrupted" as const };
    const subscribe = vi.fn();
    const cancel = vi.fn().mockResolvedValue({ ...activeRun(""), status: "cancelled" as const, cancelRequested: true });
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      getAgentRun: vi.fn().mockResolvedValue({ ...activeRun(""), status: "interrupted" as const }),
      subscribeAgentRun: subscribe,
      cancelAgentRun: cancel,
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    expect(await screen.findByText("运行已暂停，正在等待恢复")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "停止回答" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledWith("r-active"));
    expect(subscribe).not.toHaveBeenCalled();
  });

  it("取消后刷新会话与消息，并解除旧 session.running 对输入区的锁定", async () => {
    const running = activeSession();
    const cancelled = { ...activeRun(""), status: "cancelled" as const, cancelRequested: true };
    const listSessions = vi.fn().mockResolvedValueOnce([running]).mockResolvedValue([{ ...running, currentRunStatus: "cancelled" as const }]);
    const listMessages = vi.fn().mockResolvedValue([]);
    const source = {
      ...demoDataSource,
      listChatSessions: listSessions,
      listChatMessages: listMessages,
      getAgentRun: vi.fn().mockResolvedValueOnce(activeRun("")).mockResolvedValue(cancelled),
      subscribeAgentRun: vi.fn(async (_runId, _handlers, options) => {
        await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true }));
      }),
      cancelAgentRun: vi.fn().mockResolvedValue(cancelled),
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    const input = await screen.findByPlaceholderText(/输入问题/);
    fireEvent.change(input, { target: { value: "取消后继续编辑" } });
    fireEvent.click(await screen.findByRole("button", { name: "停止回答" }));
    await waitFor(() => expect(listSessions).toHaveBeenCalledTimes(2));
    expect(listMessages.mock.calls.length).toBeGreaterThanOrEqual(2);
    await waitFor(() => expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled());
  });

  it("A 会话慢响应不能覆盖后选择的 B 会话", async () => {
    const a: ChatSession = { id: "a", title: "会话 A", type: "library", createdAt, updatedAt: createdAt };
    const b: ChatSession = { id: "b", title: "会话 B", type: "library", createdAt, updatedAt: createdAt };
    let resolveA: ((messages: ChatMessage[]) => void) | undefined;
    const aPromise = new Promise<ChatMessage[]>((resolve) => { resolveA = resolve; });
    const message = (sessionId: string, content: string): ChatMessage => ({ id: `m-${sessionId}`, sessionId, role: "assistant", sequence: 1, status: "completed", content, citations: [], createdAt, updatedAt: createdAt });
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([a, b]),
      listChatMessages: vi.fn((sessionId: string) => sessionId === "a" ? aPromise : Promise.resolve([message("b", "B 的回答")])),
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    fireEvent.click((await screen.findByText("会话 B")).closest("button")!);
    expect(await screen.findByText("B 的回答")).toBeInTheDocument();
    resolveA?.([message("a", "A 的迟到回答")]);
    await Promise.resolve();
    expect(screen.queryByText("A 的迟到回答")).not.toBeInTheDocument();
    expect(screen.getByText("B 的回答")).toBeInTheDocument();
  });

  it("切换会话并 abort 后忽略旧订阅迟到回调", async () => {
    const a: ChatSession = { id: "a", title: "运行中的 A", type: "library", currentRunId: "run-a", currentRunStatus: "running", createdAt, updatedAt: createdAt };
    const b: ChatSession = { id: "b", title: "会话 B", type: "library", createdAt, updatedAt: createdAt };
    let oldHandlers: Parameters<typeof demoDataSource.subscribeAgentRun>[1] | undefined;
    let oldSignal: AbortSignal | undefined;
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([a, b]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      getAgentRun: vi.fn().mockResolvedValue({ ...activeRun(""), runId: "run-a", sessionId: "a" }),
      subscribeAgentRun: vi.fn(async (_runId, handlers, options) => {
        oldHandlers = handlers;
        oldSignal = options?.signal;
        await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true }));
      }),
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    await waitFor(() => expect(oldHandlers).toBeDefined());
    fireEvent.click(screen.getByText("会话 B").closest("button")!);
    await waitFor(() => expect(oldSignal?.aborted).toBe(true));
    oldHandlers?.onAnswerUpdate?.("A 的迟到流事件");
    oldHandlers?.onRunUpdate?.({ ...activeRun("A 的迟到流事件"), runId: "run-a", sessionId: "a", status: "completed" });
    expect(screen.queryByText("A 的迟到流事件")).not.toBeInTheDocument();
    expect(screen.getAllByText("会话 B").length).toBeGreaterThan(0);
  });

  it("POST 202 后状态同步失败仍只保留一个 pending Run，禁止新 key 重提", async () => {
    const session: ChatSession = { id: "s1", title: "新对话", type: "library", createdAt, updatedAt: createdAt };
    const submit = vi.fn().mockResolvedValue({ sessionId: "s1", messageId: "m1", runId: "r1", status: "pending", replayed: false });
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: vi.fn().mockResolvedValue([]),
      updateChatSession: vi.fn().mockResolvedValue({ ...session, title: "已受理问题" }),
      submitChatMessage: submit,
      getAgentRun: vi.fn().mockRejectedValue(new Error("同步暂不可用")),
      subscribeAgentRun: vi.fn(async (_runId, _handlers, options) => { await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true })); }),
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    const input = await screen.findByPlaceholderText(/输入问题/);
    fireEvent.change(input, { target: { value: "已受理问题" } });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/状态(?:正在|暂时无法)恢复/);
    expect(await screen.findByRole("button", { name: "停止回答" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    expect(submit).toHaveBeenCalledTimes(1);
  });

  it("终态权威消息刷新失败时继续只显示一份 live 回答", async () => {
    const session = activeSession();
    const partial: ChatMessage = { id: "m-partial", sessionId: session.id, role: "assistant", sequence: 2, status: "streaming", content: "第一段", citations: [], runId: "r-active", createdAt, updatedAt: createdAt };
    const listMessages = vi.fn().mockResolvedValueOnce([partial]).mockRejectedValueOnce(new Error("刷新失败"));
    const source = {
      ...demoDataSource,
      listChatSessions: vi.fn().mockResolvedValue([session]),
      listChatMessages: listMessages,
      getAgentRun: vi.fn().mockResolvedValue(activeRun("第一段\n\n第二段")),
      subscribeAgentRun: vi.fn(async (_runId, handlers) => {
        handlers.onAnswerUpdate?.("第一段\n\n第二段");
        handlers.onRunUpdate?.({ ...activeRun("第一段\n\n第二段"), status: "completed" });
      }),
    };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    expect(await screen.findByText("第二段")).toBeInTheDocument();
    await screen.findByRole("alert");
    expect(screen.getAllByText("第一段")).toHaveLength(1);
    expect(screen.getAllByText("第二段")).toHaveLength(1);
  });

  it.each(["approve", "reject"])("interrupted 可提交 %s 决定后恢复订阅", async (decision) => {
    const session = { ...activeSession(), currentRunStatus: "interrupted" as const };
    const interrupted = { ...activeRun(""), status: "interrupted" as const, pendingAction: { actionId: "action-1", type: "arxiv_import", riskMessage: "将导入候选论文", allowedDecisions: ["approve", "reject"], candidates: [{ arxivId: "2601.00001", title: "候选论文" }] } };
    const resume = vi.fn().mockResolvedValue({ ...interrupted, status: "pending", pendingAction: undefined });
    const subscribe = vi.fn(async (_runId, _handlers, options) => { await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true })); });
    const source = { ...demoDataSource, listChatSessions: vi.fn().mockResolvedValue([session]), listChatMessages: vi.fn().mockResolvedValue([]), getAgentRun: vi.fn().mockResolvedValue(interrupted), resumeAgentRun: resume, subscribeAgentRun: subscribe };
    render(<ChatWorkspace binding={{ type: "library" }} scopeLabel="全部文献" dataSource={source} />);
    const buttonName = decision === "approve" ? "确认导入并继续" : "不导入，继续回答";
    fireEvent.click(await screen.findByRole("button", { name: buttonName }));
    await waitFor(() => expect(resume).toHaveBeenCalledWith("r-active", "action-1", decision));
    await waitFor(() => expect(subscribe).toHaveBeenCalled());
  });
});
