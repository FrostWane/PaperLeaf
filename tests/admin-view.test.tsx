import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AdminView } from "@/components/admin-view";
import { API_BASE_URL } from "@/lib/data-source";
import { server } from "./test-server";

const modelHealth = {
  configured: true,
  providers: [{
    provider: "primary",
    purposes: {
      answer: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
      evidence_support: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
      summary: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
      translation: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
      embedding: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
      vision: { configured: false, status: "open", consecutive_failures: 3, retry_after_ms: 5000 },
      query_rewrite: { configured: true, status: "closed", consecutive_failures: 0, retry_after_ms: 0 },
    },
  }],
  policy: { timeout_seconds: 30, attempts_per_provider: 2, failure_threshold: 3, cooldown_seconds: 60 },
};

describe("AdminView 管理信息语义", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_DATA_MODE", "real");
    server.use(
      http.get(`${API_BASE_URL}/admin/users`, () => HttpResponse.json([
        { id: "admin-1", email: "only-admin@example.org", role: "admin", active: true },
        { id: "user-1", email: "reader@example.org", role: "user", active: false },
      ])),
      http.get(`${API_BASE_URL}/admin/model-health`, () => HttpResponse.json(modelHealth)),
      http.get(`${API_BASE_URL}/admin/jobs`, () => HttpResponse.json([
        { id: "queued", type: "agent_run", status: "queued", progress: 0, attempts: 0, max_attempts: 3 },
        { id: "running", type: "parse_pdf", status: "running", progress: 68, attempts: 1, max_attempts: 3 },
        { id: "completed", type: "import_arxiv", status: "completed", progress: 100, attempts: 1, max_attempts: 3 },
        { id: "failed", type: "translate_paper", status: "failed", progress: 42, attempts: 3, max_attempts: 3, error_code: "MODEL_TIMEOUT" },
      ])),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
  });

  it("用明确按钮和中文说明展示 AI 能力与任务状态", async () => {
    render(<AdminView />);

    expect(await screen.findByRole("button", { name: "停用用户 only-admin" })).toHaveTextContent("停用用户");
    expect(screen.getByRole("button", { name: "启用用户 reader" })).toHaveTextContent("启用用户");
    expect(screen.getByRole("heading", { name: "AI 能力状态" })).toBeInTheDocument();
    expect(screen.getByText("回答生成")).toHaveAttribute("title", "根据检索到的论文证据组织回答");
    expect(screen.getByText("全文翻译")).toHaveAttribute("title", "按物理页翻译已解析的论文文本");
    expect(screen.getByText("视觉 OCR")).toHaveTextContent("视觉 OCR");
    expect(screen.getByText(/暂不可用 · 尚未配置.*识别扫描版/)).toBeInTheDocument();
    expect(screen.queryByText("其他 AI 能力")).not.toBeInTheDocument();

    const jobs = screen.getByRole("heading", { name: "后台任务" }).closest("section");
    expect(jobs).not.toBeNull();
    const jobArea = within(jobs as HTMLElement);
    expect(jobArea.getByText("解析 PDF")).toBeInTheDocument();
    expect(jobArea.getByText("处理进度 68% · 第 1 次执行，最多 3 次")).toBeInTheDocument();
    expect(jobArea.getAllByText("等待处理").length).toBeGreaterThanOrEqual(1);
    expect(jobArea.getByRole("progressbar", { name: "处理进度 68%" })).toHaveAttribute("aria-valuenow", "68");
    expect(jobArea.getByText("导入论文")).toBeInTheDocument();
    const completed = jobArea.getByText("已完成", { selector: "small" }).closest(".job-row");
    expect(completed).not.toBeNull();
    expect(completed).not.toHaveTextContent("100%");
    expect(completed).not.toHaveTextContent("1/3");
    expect(jobArea.getByText(/失败原因：AI 服务响应超时/)).toBeInTheDocument();
  });

  it("停用前确认，并展示后端返回的具体禁止原因", async () => {
    server.use(http.patch(`${API_BASE_URL}/admin/users/admin-1`, () => HttpResponse.json(
      { detail: "不能停用或降级最后一名管理员" },
      { status: 409 },
    )));

    render(<AdminView />);
    fireEvent.click(await screen.findByRole("button", { name: "停用用户 only-admin" }));
    expect(screen.getByRole("dialog", { name: "确认停用用户" })).toHaveTextContent("停用后，该用户的现有会话将失效");
    fireEvent.click(screen.getByRole("button", { name: "确认停用" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("不能停用或降级最后一名管理员"));
  });

  it("取消停用确认时不会发送状态修改请求", async () => {
    let updates = 0;
    server.use(http.patch(`${API_BASE_URL}/admin/users/admin-1`, () => {
      updates += 1;
      return HttpResponse.json({ id: "admin-1", email: "only-admin@example.org", role: "admin", active: false });
    }));

    render(<AdminView />);
    fireEvent.click(await screen.findByRole("button", { name: "停用用户 only-admin" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog", { name: "确认停用用户" })).not.toBeInTheDocument();
    await waitFor(() => expect(updates).toBe(0));
  });
});
