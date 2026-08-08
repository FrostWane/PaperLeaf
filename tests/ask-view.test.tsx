import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AskView } from "@/components/ask-view";
import { API_BASE_URL } from "@/lib/data-source";
import { server } from "./test-server";

const collectionPayload = [{ id: "core", name: "核心方法", parent_id: null, paper_ids: [], recursive_paper_count: 1, children: [
  { id: "child", name: "证据定位", parent_id: "core", paper_ids: ["p1"], recursive_paper_count: 1, children: [] },
] }];

describe("AskView 持久化问答", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_DATA_MODE", "real");
    localStorage.clear();
    server.use(
      http.get(`${API_BASE_URL}/papers`, () => HttpResponse.json([{ id: "p1", title: "论文甲", authors: ["作者甲"], year: 2025, page_count: 12, status: "ready" }])),
      http.get(`${API_BASE_URL}/collections`, () => HttpResponse.json(collectionPayload)),
      http.get(`${API_BASE_URL}/chat/sessions`, () => HttpResponse.json([])),
      http.get(`${API_BASE_URL}/users/me/preferences`, () => HttpResponse.json({ arxiv_search_enabled: false })),
    );
  });
  afterEach(() => { cleanup(); vi.unstubAllEnvs(); });

  it("示例问题只写入输入框并聚焦，不产生 POST", async () => {
    let posts = 0;
    server.use(http.post(`${API_BASE_URL}/chat/sessions`, () => { posts += 1; return HttpResponse.error(); }));
    render(<AskView />);
    fireEvent.click(await screen.findByRole("button", { name: /比较这些论文所采用的方法/ }));
    const input = screen.getByPlaceholderText(/输入问题/);
    expect(input).toHaveValue("比较这些论文所采用的方法与关键假设");
    expect(input).toHaveFocus();
    expect(posts).toBe(0);
  });

  it("集合范围由会话保存，提交接口只传 collection_id 而不传前端论文快照", async () => {
    let sessions: Record<string, unknown>[] = [];
    let createBody: unknown;
    let messageBody: unknown;
    server.use(
      http.get(`${API_BASE_URL}/chat/sessions`, () => HttpResponse.json(sessions)),
      http.post(`${API_BASE_URL}/chat/sessions`, async ({ request }) => {
        createBody = await request.json();
        const session = { id: "s-core", title: "比较两种方法", type: "collection", collection_id: "core", created_at: "2026-08-06T10:00:00Z", updated_at: "2026-08-06T10:00:00Z" };
        sessions = [session];
        return HttpResponse.json(session, { status: 201 });
      }),
      http.get(`${API_BASE_URL}/chat/sessions/s-core/messages`, () => HttpResponse.json([])),
      http.post(`${API_BASE_URL}/chat/sessions/s-core/messages`, async ({ request }) => {
        messageBody = await request.json();
        return HttpResponse.json({ session_id: "s-core", message_id: "m1", run_id: "r1", status: "pending", replayed: false }, { status: 202 });
      }),
      http.get(`${API_BASE_URL}/agent/runs/r1`, () => HttpResponse.json({ run_id: "r1", session_id: "s-core", status: "completed", cancel_requested: false, answer: "", citations: [], created_at: "2026-08-06T10:00:00Z", updated_at: "2026-08-06T10:00:01Z" })),
      http.get(`${API_BASE_URL}/agent/runs/r1/events`, () => new HttpResponse(
        'id: 1\nevent: run_finished\ndata: {"sequence":1,"event":"run_finished","data":{"status":"completed"}}\n\n',
        { headers: { "content-type": "text/event-stream" } },
      )),
    );
    render(<AskView />);
    fireEvent.click(await screen.findByRole("treeitem", { name: /核心方法.*1/ }));
    const input = screen.getByPlaceholderText(/输入问题/);
    fireEvent.change(input, { target: { value: "比较两种方法" } });
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(messageBody).toEqual({
      content: "比较两种方法",
      web_enabled: false,
      client_context: { active_panel: "chat", collection_id: "core" },
    }));
    expect(createBody).toEqual({ type: "collection", title: "比较两种方法", collection_id: "core" });
    expect(screen.queryByText("最近阅读")).not.toBeInTheDocument();
  });
});
