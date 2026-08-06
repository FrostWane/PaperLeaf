import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AskView } from "@/components/ask-view";
import { API_BASE_URL } from "@/lib/data-source";
import { server } from "./test-server";

const question = "论文如何解释证据位置偏差？";

describe("AskView 真实问答状态", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_DATA_MODE", "real");
    server.use(
      http.get(`${API_BASE_URL}/papers`, () => HttpResponse.json([
        { id: "p1", title: "论文甲", authors: ["作者甲"], year: 2025, page_count: 12, status: "ready", last_opened_at: "2026-08-04T10:00:00Z" },
        { id: "p2", title: "论文乙", authors: ["作者乙"], year: 2024, page_count: 8, status: "indexing" },
      ])),
      http.get(`${API_BASE_URL}/collections`, () => HttpResponse.json([
        { id: "core", name: "核心方法", parent_id: null, paper_ids: [], recursive_paper_count: 1, children: [
          { id: "child", name: "证据定位", parent_id: "core", paper_ids: ["p1"], recursive_paper_count: 1, children: [] },
        ] },
        { id: "empty", name: "空集合", parent_id: null, paper_ids: [], recursive_paper_count: 0, children: [] },
      ])),
    );
  });
  afterEach(() => { cleanup(); vi.unstubAllEnvs(); });

  it("提交后立即保留问题，并按 SSE 回答与引用事件增量更新", async () => {
    let controller: ReadableStreamDefaultController<Uint8Array> | undefined;
    server.use(http.post(`${API_BASE_URL}/chat/sessions/default/messages`, () => new HttpResponse(
      new ReadableStream<Uint8Array>({ start(nextController) { controller = nextController; } }),
      { headers: { "content-type": "text/event-stream" } },
    )));

    render(<AskView />);
    fireEvent.change(screen.getByPlaceholderText(/这些论文如何解释/), { target: { value: question } });
    fireEvent.click(screen.getByRole("button", { name: "开始提问" }));

    expect(await screen.findByRole("heading", { name: question })).toBeInTheDocument();
    expect(screen.getByText("正在准备基于文献证据的回答…")).toBeInTheDocument();
    expect(controller).toBeDefined();

    const encoder = new TextEncoder();
    await act(async () => {
      controller?.enqueue(encoder.encode('event: message_delta\ndata: {"event":"message_delta","run_id":"r1","data":{"delta":"证据显示位置会影响回答"}}\n\n'));
    });
    expect(await screen.findByText("证据显示位置会影响回答")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /论文甲/ })).not.toBeInTheDocument();

    await act(async () => {
      controller?.enqueue(encoder.encode('event: citation\ndata: {"event":"citation","run_id":"r1","data":{"paper_id":"p1","paper_title":"论文甲","physical_page":4,"chunk_id":"p1:p4:c0","excerpt":"原文证据"}}\n\n'));
    });
    expect(await screen.findByRole("link", { name: /论文甲/ })).toHaveTextContent("PDF 4");

    await act(async () => {
      controller?.enqueue(encoder.encode('event: run_finished\ndata: {"event":"run_finished","run_id":"r1","data":{"status":"completed"}}\n\n'));
      controller?.close();
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "开始提问" })).toBeEnabled());
  });

  it("服务端运行错误可见，并保留已提交问题和已收到的部分回答", async () => {
    server.use(http.post(`${API_BASE_URL}/chat/sessions/default/messages`, () => new HttpResponse(
      'event: message_delta\ndata: {"event":"message_delta","run_id":"r2","data":{"delta":"已收到部分证据"}}\n\nevent: error\ndata: {"event":"error","run_id":"r2","data":{"message":"证据服务暂时不可用"}}\n\n',
      { headers: { "content-type": "text/event-stream" } },
    )));

    render(<AskView />);
    fireEvent.change(screen.getByPlaceholderText(/这些论文如何解释/), { target: { value: question } });
    fireEvent.click(screen.getByRole("button", { name: "开始提问" }));

    expect(await screen.findByRole("heading", { name: question })).toBeInTheDocument();
    expect(await screen.findByText("已收到部分证据")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("证据服务暂时不可用");
  });

  it("HTTP 服务错误可见且不清空问题", async () => {
    server.use(http.post(`${API_BASE_URL}/chat/sessions/default/messages`, () => new HttpResponse(null, { status: 503 })));

    render(<AskView />);
    fireEvent.change(screen.getByPlaceholderText(/这些论文如何解释/), { target: { value: question } });
    fireEvent.click(screen.getByRole("button", { name: "开始提问" }));

    expect(await screen.findByRole("heading", { name: question })).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("问答服务暂时不可用");
  });

  it("网络错误可见且不清空问题", async () => {
    server.use(http.post(`${API_BASE_URL}/chat/sessions/default/messages`, () => HttpResponse.error()));

    render(<AskView />);
    fireEvent.change(screen.getByPlaceholderText(/这些论文如何解释/), { target: { value: question } });
    fireEvent.click(screen.getByRole("button", { name: "开始提问" }));

    expect(await screen.findByRole("heading", { name: question })).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("网络连接失败");
  });

  it("提交集合 ID 由服务端递归解析文献范围", async () => {
    let payload: Record<string, unknown> | undefined;
    server.use(http.post(`${API_BASE_URL}/chat/sessions/default/messages`, async ({ request }) => {
      payload = await request.json() as Record<string, unknown>;
      return new HttpResponse(
        'event: run_finished\ndata: {"event":"run_finished","run_id":"r3","data":{"status":"completed"}}\n\n',
        { headers: { "content-type": "text/event-stream" } },
      );
    }));

    render(<AskView />);
    const collectionButton = await screen.findByRole("treeitem", { name: /核心方法.*1/ });
    fireEvent.click(collectionButton);
    fireEvent.change(screen.getByPlaceholderText(/这些论文如何解释/), { target: { value: question } });
    fireEvent.click(screen.getByRole("button", { name: "开始提问" }));

    await waitFor(() => expect(payload).toMatchObject({ scope: "collection", selected_collection_id: "core", selected_paper_ids: [] }));
    expect(screen.queryByText("最近阅读")).not.toBeInTheDocument();
  });

  it("点击示例问题会写入输入框", async () => {
    render(<AskView />);
    fireEvent.click(screen.getByRole("button", { name: /比较 Transformer 与 RNN/ }));
    expect(screen.getByPlaceholderText(/这些论文如何解释/)).toHaveValue("比较 Transformer 与 RNN 的计算路径");
  });
});
