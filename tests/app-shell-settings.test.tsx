import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "@/components/app-shell";
import { resetCurrentUserStateForTests } from "@/components/current-user-provider";
import { SettingsView } from "@/components/settings-view";
import { server } from "./test-server";

const api = "/api/v1";
const preferences = {
  font_scale: "standard",
  pdf_zoom: 100,
  left_panel_open: true,
  assistant_panel_open: true,
  translation_language: "zh-CN",
  arxiv_search_enabled: false,
};

describe("AppShell 账户与角色导航", () => {
  beforeEach(() => vi.stubEnv("NEXT_PUBLIC_DATA_MODE", "real"));
  afterEach(() => {
    cleanup();
    resetCurrentUserStateForTests();
    vi.unstubAllEnvs();
    document.documentElement.removeAttribute("data-font-scale");
    document.cookie = "paperleaf_csrf=; Max-Age=0; path=/";
  });

  it("页面外壳重新挂载时复用已验证身份，不闪回未登录状态", async () => {
    let requests = 0;
    server.use(http.get(`${api}/auth/me`, () => {
      requests += 1;
      return HttpResponse.json({ id: "a1", email: "admin@example.org", display_name: "管理员", role: "admin", active: true, must_change_password: false, preferences });
    }));

    const first = render(<AppShell active="/library" title="文献库"><p>正文</p></AppShell>);
    expect(await screen.findByText("admin@example.org")).toBeInTheDocument();
    first.unmount();
    render(<AppShell active="/ask" title="跨文献提问"><p>正文</p></AppShell>);

    expect(screen.getByText("admin@example.org")).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "主导航" })).getByRole("link", { name: /管理/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("正在验证账户")).not.toBeInTheDocument();
    expect(requests).toBe(1);
  });

  it("显示真实用户并对普通用户隐藏管理入口", async () => {
    server.use(http.get(`${api}/auth/me`, () => HttpResponse.json({
      id: "u1",
      email: "reader@example.org",
      display_name: "陈博士",
      role: "user",
      active: true,
      must_change_password: false,
      preferences: { ...preferences, font_scale: "large" },
    })));

    render(<AppShell active="/library" title="文献库"><p>正文</p></AppShell>);

    expect(await screen.findByText("陈博士")).toBeInTheDocument();
    expect(screen.getByText("reader@example.org")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "管理" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "通知" })).not.toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute("data-font-scale", "large");
  });

  it("管理员可见管理入口，并能从账户菜单退出", async () => {
    let logoutRequests = 0;
    const onLoggedOut = vi.fn();
    document.cookie = "paperleaf_csrf=csrf-test; path=/";
    server.use(
      http.get(`${api}/auth/me`, () => HttpResponse.json({ id: "a1", email: "admin@example.org", display_name: "管理员", role: "admin", active: true, must_change_password: false, preferences })),
      http.post(`${api}/auth/logout`, ({ request }) => {
        expect(request.headers.get("X-CSRF-Token")).toBe("csrf-test");
        logoutRequests += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    render(<AppShell active="/admin" title="管理" onLoggedOut={onLoggedOut}><p>正文</p></AppShell>);
    const navigation = await screen.findByRole("navigation", { name: "主导航" });
    expect(within(navigation).getByRole("link", { name: /管理/ })).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("打开 管理员 的账户菜单"));
    expect(screen.getByRole("link", { name: /个人设置/ })).toHaveAttribute("href", "/settings");
    fireEvent.click(screen.getByRole("button", { name: "退出登录" }));

    await waitFor(() => expect(logoutRequests).toBe(1));
    expect(onLoggedOut).toHaveBeenCalledOnce();
  });
});

describe("SettingsView 真实偏好", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_DATA_MODE", "real");
    server.use(http.get(`${api}/memories`, () => HttpResponse.json({ items: [], total: 0, active: 0, capacity: 200 })));
  });
  afterEach(() => {
    cleanup();
    resetCurrentUserStateForTests();
    vi.unstubAllEnvs();
    document.documentElement.removeAttribute("data-font-scale");
    document.cookie = "paperleaf_csrf=; Max-Age=0; path=/";
  });

  it("读取、编辑并持久化全部个人偏好", async () => {
    let payload: Record<string, unknown> | undefined;
    document.cookie = "paperleaf_csrf=settings-csrf; path=/";
    server.use(
      http.get(`${api}/users/me/preferences`, () => HttpResponse.json({ display_name: "原昵称", ...preferences })),
      http.patch(`${api}/users/me/preferences`, async ({ request }) => {
        expect(request.headers.get("X-CSRF-Token")).toBe("settings-csrf");
        payload = await request.json() as Record<string, unknown>;
        return HttpResponse.json(payload);
      }),
    );

    render(<SettingsView />);
    const nameInput = await screen.findByDisplayValue("原昵称");
    fireEvent.change(nameInput, { target: { value: "新昵称" } });
    fireEvent.click(screen.getByRole("radio", { name: /大适合 2K/ }));
    fireEvent.change(screen.getByLabelText(/默认 PDF 缩放/), { target: { value: "130" } });
    fireEvent.click(screen.getByRole("switch", { name: "默认展开文献资料" }));
    fireEvent.click(screen.getByRole("switch", { name: "允许联网学术搜索" }));
    fireEvent.change(screen.getByLabelText(/全文翻译目标语言/), { target: { value: "ja" } });
    fireEvent.click(screen.getByRole("button", { name: "保存个人设置" }));

    await waitFor(() => expect(payload).toMatchObject({
      display_name: "新昵称",
      font_scale: "large",
      pdf_zoom: 130,
      left_panel_open: false,
      assistant_panel_open: true,
      translation_language: "ja",
      arxiv_search_enabled: true,
    }));
    expect(document.documentElement).toHaveAttribute("data-font-scale", "large");
    expect(await screen.findByText(/个人设置已保存/)).toBeInTheDocument();
  });

  it("展示服务端返回的具体保存失败原因", async () => {
    server.use(
      http.get(`${api}/users/me/preferences`, () => HttpResponse.json({ display_name: "研究者", ...preferences })),
      http.patch(`${api}/users/me/preferences`, () => HttpResponse.json({ detail: "昵称已被占用" }, { status: 409 })),
    );
    render(<SettingsView />);
    await screen.findByDisplayValue("研究者");
    fireEvent.click(screen.getByRole("button", { name: "保存个人设置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("昵称已被占用");
  });

  it("展示对象 detail 中的中文失败原因", async () => {
    server.use(
      http.get(`${api}/users/me/preferences`, () => HttpResponse.json({ display_name: "研究者", ...preferences })),
      http.patch(`${api}/users/me/preferences`, () => HttpResponse.json({ detail: { code: "PASSWORD_CHANGE_REQUIRED", message: "请先修改临时密码" } }, { status: 403 })),
    );
    render(<SettingsView />);
    await screen.findByDisplayValue("研究者");
    fireEvent.click(screen.getByRole("button", { name: "保存个人设置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("请先修改临时密码");
  });

  it("偏好读取失败时禁止用默认值覆盖真实账户", async () => {
    let updates = 0;
    server.use(
      http.get(`${api}/users/me/preferences`, () => HttpResponse.json({ detail: "个人设置暂时不可用" }, { status: 503 })),
      http.patch(`${api}/users/me/preferences`, () => {
        updates += 1;
        return HttpResponse.json({ display_name: "不应保存", ...preferences });
      }),
    );
    render(<SettingsView />);
    expect(await screen.findByRole("alert")).toHaveTextContent("个人设置暂时不可用");
    expect(screen.getByRole("button", { name: "保存个人设置" })).toBeDisabled();
    expect(screen.getByLabelText(/默认 PDF 缩放/)).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "保存个人设置" }));
    expect(updates).toBe(0);
  });

  it("展示、停用并删除用户自己的长期记忆", async () => {
    let patched: Record<string, unknown> | undefined;
    let deleted = 0;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    server.use(
      http.get(`${api}/users/me/preferences`, () => HttpResponse.json({ display_name: "研究者", ...preferences, memory_enabled: true })),
      http.get(`${api}/memories`, () => HttpResponse.json({
        items: [{ id: "m1", type: "research_interest", value: "药物靶点亲和力预测", confidence: 0.97, source_kind: "stated", source_excerpt: "我的研究方向是药物靶点亲和力预测", pinned: false, enabled: true, created_at: "2026-08-08T00:00:00Z", updated_at: "2026-08-08T00:00:00Z" }],
        total: 1, active: 1, capacity: 200,
      })),
      http.patch(`${api}/memories/m1`, async ({ request }) => {
        patched = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ id: "m1", type: "research_interest", value: "药物靶点亲和力预测", confidence: 0.97, source_kind: "stated", source_excerpt: "我的研究方向是药物靶点亲和力预测", pinned: false, enabled: false, created_at: "2026-08-08T00:00:00Z", updated_at: "2026-08-08T01:00:00Z" });
      }),
      http.delete(`${api}/memories/m1`, () => { deleted += 1; return new HttpResponse(null, { status: 204 }); }),
    );
    render(<SettingsView />);

    expect(await screen.findByDisplayValue("药物靶点亲和力预测")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "停用记忆" }));
    await waitFor(() => expect(patched).toEqual({ enabled: false }));
    fireEvent.click(screen.getByRole("button", { name: "删除记忆" }));
    await waitFor(() => expect(deleted).toBe(1));
    expect(screen.queryByDisplayValue("药物靶点亲和力预测")).not.toBeInTheDocument();
  });
});
