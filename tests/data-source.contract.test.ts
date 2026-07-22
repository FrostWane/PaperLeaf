import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { API_BASE_URL, changePassword, login, realDataSource } from "@/lib/data-source";
import { server } from "./test-server";

describe("真实 API 契约", () => {
  it("登录发送后端需要的 email/password 并携带 cookie 模式", async () => {
    let payload: unknown;
    server.use(http.post(`${API_BASE_URL}/auth/login`, async ({ request }) => { payload = await request.json(); return HttpResponse.json({ id: "u1", email: "a@b.com" }); }));
    await login("a@b.com", "12345678");
    expect(payload).toEqual({ email: "a@b.com", password: "12345678" });
  });

  it("首次改密使用后端字段并携带 CSRF", async () => {
    document.cookie = "paperleaf_csrf=password-token; path=/";
    let payload: unknown;
    let csrf = "";
    server.use(http.post(`${API_BASE_URL}/auth/change-password`, async ({ request }) => {
      payload = await request.json();
      csrf = request.headers.get("X-CSRF-Token") ?? "";
      return HttpResponse.json({ id: "u1", email: "a@b.com", role: "user", active: true, must_change_password: false });
    }));
    const user = await changePassword("temporary-password", "a-new-strong-password");
    expect(payload).toEqual({ current_password: "temporary-password", new_password: "a-new-strong-password" });
    expect(csrf).toBe("password-token");
    expect(user.mustChangePassword).toBe(false);
  });

  it("读取单篇文献时映射真实页数与 arXiv 元数据", async () => {
    server.use(http.get(`${API_BASE_URL}/papers/p1`, () => HttpResponse.json({ id: "p1", title: "论文", authors: ["作者甲"], year: 2025, page_count: 12, status: "ready", abstract: "摘要", arxiv_id: "2501.00001" })));
    await expect(realDataSource.getPaper("p1")).resolves.toMatchObject({ id: "p1", authors: "作者甲", pages: 12, status: "ready", arxivId: "2501.00001" });
  });

  it("上传使用 multipart 和 CSRF 请求头", async () => {
    document.cookie = "paperleaf_csrf=test-token; path=/";
    let uploadedType = ""; let csrf = "";
    server.use(http.post(`${API_BASE_URL}/papers`, async ({ request }) => { const data = await request.formData(); uploadedType = (data.get("file") as File).type; csrf = request.headers.get("X-CSRF-Token") ?? ""; return HttpResponse.json({ id: "p1", title: "论文", page_count: 3 }, { status: 201 }); }));
    const paper = await realDataSource.upload(new File(["%PDF-1.7"], "论文.pdf", { type: "application/pdf" }), () => undefined);
    expect(uploadedType).toBe("application/pdf"); expect(csrf).toBe("test-token"); expect(paper.id).toBe("p1");
  });

  it("Chat 使用 content/scope/selected_paper_ids/web_enabled 并聚合 SSE", async () => {
    document.cookie = "paperleaf_csrf=chat-token; path=/";
    let payload: Record<string, unknown> = {};
    server.use(http.post(`${API_BASE_URL}/chat/sessions/default/messages`, async ({ request }) => { payload = await request.json() as Record<string, unknown>; return new HttpResponse('event: message_delta\ndata: {"event":"message_delta","run_id":"r1","data":{"delta":"有依据"}}\n\nevent: citation\ndata: {"event":"citation","run_id":"r1","data":{"paper_id":"p1","paper_title":"论文","physical_page":2,"chunk_id":"c1","quote":"原文"}}\n\nevent: run_finished\ndata: {"event":"run_finished","run_id":"r1","data":{"status":"completed"}}\n\n', { headers: { "content-type": "text/event-stream" } }); }));
    const answer = await realDataSource.ask("为什么？", ["p1"]);
    expect(payload).toEqual({ content: "为什么？", scope: "paper", selected_paper_ids: ["p1"], web_enabled: false });
    expect(answer.answer).toBe("有依据"); expect(answer.citations[0]).toMatchObject({ paperId: "p1", page: 2, chunkId: "c1" });
  });
});
