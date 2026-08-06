import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatWorkspace } from "@/components/chat-workspace";
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
    fireEvent.click(screen.getByRole("button", { name: "取消运行" }));
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
    fireEvent.click(await screen.findByRole("button", { name: "取消运行" }));
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
    expect(await screen.findByRole("button", { name: "取消运行" })).toBeInTheDocument();
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
