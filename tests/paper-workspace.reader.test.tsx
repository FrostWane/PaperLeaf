import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PaperWorkspace } from "@/components/paper-workspace";
import { demoDataSource } from "@/lib/data-source";
import { server } from "./test-server";

class TestResizeObserver {
  observe() { /* jsdom 不需要实际测量。 */ }
  unobserve() { /* jsdom 不需要实际测量。 */ }
  disconnect() { /* jsdom 不需要实际测量。 */ }
}

describe("PaperWorkspace PDF 主视区", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.stubGlobal("crypto", { subtle: { digest: vi.fn(async () => new Uint8Array(32).buffer) } });
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it("缩放限制在 50% 到 200%，侧栏与专注切换不会丢失页码", () => {
    const { container } = render(<PaperWorkspace demo paperId="attention" initialPage={2} />);
    const desktopRoot = container.querySelector(".workspace-desktop") as HTMLElement;
    let desktop = within(desktopRoot);

    expect(desktop.getByLabelText("第 2 页，共 15 页")).toBeVisible();
    const zoomOut = desktop.getByRole("button", { name: "缩小 PDF" });
    for (let index = 0; index < 8; index += 1) fireEvent.click(zoomOut);
    expect(desktop.getByText("50%", { selector: "output" })).toBeVisible();
    expect(zoomOut).toBeDisabled();

    const zoomIn = desktop.getByRole("button", { name: "放大 PDF" });
    for (let index = 0; index < 18; index += 1) fireEvent.click(zoomIn);
    expect(desktop.getByText("200%", { selector: "output" })).toBeVisible();
    expect(zoomIn).toBeDisabled();

    fireEvent.click(desktop.getByRole("button", { name: "适合宽度" }));
    expect(desktop.getByRole("button", { name: "适合宽度" })).toHaveAttribute("aria-pressed", "true");
    expect(desktop.getByText("自适应", { selector: "output" })).toBeVisible();
    fireEvent.click(desktop.getByRole("button", { name: "下一页" }));
    expect(desktop.getByLabelText("第 3 页，共 15 页")).toBeVisible();

    const pdfNode = desktop.getByLabelText("模拟 PDF 第 3 页");
    fireEvent.click(desktop.getByText("阅读布局"));
    fireEvent.click(desktop.getByRole("button", { name: "收起资料栏" }));
    desktop = within(desktopRoot);
    expect(desktop.queryByLabelText("论文信息")).not.toBeInTheDocument();
    expect(desktop.getByRole("button", { name: "显示资料栏" })).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(desktop.getByRole("button", { name: "专注阅读" }));
    desktop = within(desktopRoot);
    expect(desktop.queryByLabelText("论文助手")).not.toBeInTheDocument();
    expect(desktop.getByLabelText("第 3 页，共 15 页")).toBeVisible();
    expect(desktop.getByLabelText("模拟 PDF 第 3 页")).toBe(pdfNode);
    fireEvent.click(desktop.getByRole("button", { name: "退出专注阅读" }));
    desktop = within(desktopRoot);
    expect(desktop.queryByLabelText("论文信息")).not.toBeInTheDocument();
    expect(desktop.getByLabelText("论文助手")).toBeVisible();
    expect(desktop.getByLabelText("第 3 页，共 15 页")).toBeVisible();
    expect(desktop.getByLabelText("模拟 PDF 第 3 页")).toBe(pdfNode);
  });

  it("拖选 PDF 原文后显示明确反馈，并可清除或在换页时自动清除", async () => {
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
    expect(desktop.getByRole("button", { name: "清除已选原文" })).toBeVisible();
    expect(desktop.getByRole("button", { name: "已选原文" })).toBeVisible();

    fireEvent.click(desktop.getByRole("button", { name: "清除已选原文" }));
    expect(desktop.queryByText(/已选原文 10 字/)).not.toBeInTheDocument();
    expect(desktop.queryByRole("button", { name: "已选原文" })).not.toBeInTheDocument();

    fireEvent.mouseUp(original);
    expect(await desktop.findByText("已选原文 10 字", { selector: ".reader-selection-status" })).toBeVisible();
    fireEvent.click(desktop.getByRole("button", { name: "下一页" }));
    expect(desktop.queryByText(/已选原文 10 字/)).not.toBeInTheDocument();
    expect(desktop.queryByRole("button", { name: "已选原文" })).not.toBeInTheDocument();
  });

  it("从回答引用跳到其他物理页时清理旧页选文", async () => {
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
    fireEvent.click(await assistant.findByTitle(/PDF 第 6 页/));

    expect(desktop.getByLabelText("第 6 页，共 15 页")).toBeVisible();
    expect(desktop.queryByText(/已选原文 10 字/)).not.toBeInTheDocument();
    expect(assistant.queryByRole("button", { name: "已选原文" })).not.toBeInTheDocument();
  });

  it("确认时传递当前页优先级，译文中的 HTML 只作为纯文本展示并可取消", async () => {
    const create = vi.spyOn(demoDataSource, "createPaperTranslation");
    vi.spyOn(demoDataSource, "getPaperTranslationPage").mockImplementation(async (_paperId, _translationId, page) => ({ page, status: "completed", text: page === 2 ? '<script>alert("x")</script>\n\n安全译文' : `第 ${page} 页缓存译文` }));
    const { container } = render(<PaperWorkspace demo paperId="attention" initialPage={2} />);
    const desktop = within(container.querySelector(".workspace-desktop") as HTMLElement);

    fireEvent.click(desktop.getByRole("button", { name: "翻译全文" }));
    expect(screen.getByRole("dialog", { name: "翻译整篇论文" })).toHaveTextContent("15 页原文");
    expect(screen.getByLabelText("目标语言")).toHaveValue("zh-CN");
    fireEvent.click(screen.getByRole("button", { name: "确认并开始翻译" }));

    await waitFor(() => expect(create).toHaveBeenCalledWith("attention", "zh-CN", 2));
    const translationPane = await desktop.findByLabelText("简体中文译文，第 2 页");
    expect(translationPane).not.toHaveTextContent('alert("x")');
    expect(await within(translationPane).findByText("安全译文")).toBeVisible();
    expect(translationPane.querySelector("script")).toBeNull();
    fireEvent.click(within(translationPane).getByRole("button", { name: "取消后台翻译" }));
    expect(within(translationPane).getByRole("button", { name: "正在取消…" })).toBeDisabled();
    await waitFor(() => expect(within(translationPane).getByText(/翻译任务已取消/)).toBeVisible());
    fireEvent.click(desktop.getByRole("button", { name: "下一页" }));
    expect(await within(translationPane).findByText("第 3 页缓存译文")).toBeVisible();
    expect(within(translationPane).getByText("第 3 / 15 页")).toBeVisible();
    fireEvent.click(within(translationPane).getByRole("button", { name: "下一页译文" }));
    expect(await within(translationPane).findByText("第 4 页缓存译文")).toBeVisible();
  });

  it("后台完成当前页后无需刷新即可显示译文与进度", async () => {
    let pageReads = 0;
    vi.spyOn(demoDataSource, "createPaperTranslation").mockResolvedValue({ id: "live-translation", paperId: "attention", targetLanguage: "zh-CN", status: "running", progress: 0, completedPages: 0, failedPages: 0, totalPages: 15 });
    vi.spyOn(demoDataSource, "getPaperTranslation").mockResolvedValue({ id: "live-translation", paperId: "attention", targetLanguage: "zh-CN", status: "running", progress: 7, completedPages: 1, failedPages: 0, totalPages: 15 });
    vi.spyOn(demoDataSource, "getPaperTranslationPage").mockImplementation(async (_paperId, _translationId, page) => {
      pageReads += 1;
      return pageReads === 1
        ? { page, status: "running", text: "" }
        : { page, status: "completed", text: "无需刷新出现的译文" };
    });
    const { container } = render(<PaperWorkspace demo paperId="attention" initialPage={2} />);
    const desktop = within(container.querySelector(".workspace-desktop") as HTMLElement);
    fireEvent.click(desktop.getByRole("button", { name: "翻译全文" }));
    fireEvent.click(screen.getByRole("button", { name: "确认并开始翻译" }));
    expect(await desktop.findByText("正在翻译第 2 页…")).toBeVisible();
    expect(await desktop.findByText("无需刷新出现的译文", {}, { timeout: 2_500 })).toBeVisible();
    expect(pageReads).toBeGreaterThanOrEqual(2);
    expect(desktop.getByText("正在翻译 · 1/15 页")).toBeVisible();
  });

  it("完成后可确认重新翻译并强制刷新缓存", async () => {
    const create = vi.spyOn(demoDataSource, "createPaperTranslation")
      .mockResolvedValueOnce({ id: "done-translation", paperId: "attention", targetLanguage: "zh-CN", status: "completed", progress: 100, completedPages: 15, failedPages: 0, totalPages: 15 })
      .mockResolvedValueOnce({ id: "done-translation", paperId: "attention", targetLanguage: "zh-CN", status: "queued", progress: 0, completedPages: 0, failedPages: 0, totalPages: 15 });
    vi.spyOn(demoDataSource, "getPaperTranslationPage").mockResolvedValue({ page: 2, status: "completed", text: "旧译文" });
    const { container } = render(<PaperWorkspace demo paperId="attention" initialPage={2} />);
    const desktop = within(container.querySelector(".workspace-desktop") as HTMLElement);
    fireEvent.click(desktop.getByRole("button", { name: "翻译全文" }));
    fireEvent.click(screen.getByRole("button", { name: "确认并开始翻译" }));
    expect(await desktop.findByRole("button", { name: "重新翻译" })).toBeVisible();
    fireEvent.click(desktop.getByRole("button", { name: "重新翻译" }));
    fireEvent.click(screen.getByRole("button", { name: "确认并开始翻译" }));
    await waitFor(() => expect(create).toHaveBeenLastCalledWith("attention", "zh-CN", 2, { refresh: true }));
  });

  it("缩放偏好去抖保存，并在重新挂载阅读器后恢复", async () => {
    let savedZoom = 130;
    let zoomUpdates = 0;
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json({
        id: "u1",
        email: "reader@example.org",
        display_name: "研究者",
        role: "user",
        active: true,
        must_change_password: false,
        preferences: { font_scale: "standard", pdf_zoom: savedZoom, left_panel_open: true, assistant_panel_open: true, translation_language: "zh-CN", arxiv_search_enabled: false },
      })),
      http.patch("/api/v1/users/me/preferences", async ({ request }) => {
        const payload = await request.json() as Record<string, unknown>;
        if (typeof payload.pdf_zoom === "number") { savedZoom = payload.pdf_zoom; zoomUpdates += 1; }
        return HttpResponse.json({ display_name: "研究者", font_scale: "standard", pdf_zoom: savedZoom, left_panel_open: true, assistant_panel_open: true, translation_language: "zh-CN", arxiv_search_enabled: false });
      }),
    );

    const first = render(<PaperWorkspace paperId="attention" />);
    let desktop = within(first.container.querySelector(".workspace-desktop") as HTMLElement);
    expect(await desktop.findByText("130%", { selector: "output" })).toBeVisible();
    fireEvent.click(desktop.getByRole("button", { name: "放大 PDF" }));
    await waitFor(() => expect(savedZoom).toBe(140), { timeout: 1_500 });
    expect(zoomUpdates).toBe(1);
    first.unmount();

    const second = render(<PaperWorkspace paperId="attention" />);
    desktop = within(second.container.querySelector(".workspace-desktop") as HTMLElement);
    expect(await desktop.findByText("140%", { selector: "output" })).toBeVisible();
  });

  it("无文本页给出明确状态", async () => {
    const { container } = render(<PaperWorkspace demo paperId="attention" initialPage={7} />);
    const desktop = within(container.querySelector(".workspace-desktop") as HTMLElement);
    fireEvent.click(desktop.getByRole("button", { name: "翻译全文" }));
    fireEvent.click(screen.getByRole("button", { name: "确认并开始翻译" }));
    expect(await desktop.findByText("此页暂无可翻译文本，可能是图片页或尚未完成 OCR。")).toBeVisible();
  });

  it("partial 是终态且只提供失败页重试，不启动轮询或取消", async () => {
    const interval = vi.spyOn(window, "setInterval");
    vi.spyOn(demoDataSource, "createPaperTranslation").mockResolvedValue({ id: "partial-1", paperId: "attention", targetLanguage: "zh-CN", status: "partial", progress: 100, completedPages: 11, failedPages: 2, totalPages: 15 });
    vi.spyOn(demoDataSource, "getPaperTranslationPage").mockResolvedValue({ page: 2, status: "completed", text: "缓存译文" });
    const { container } = render(<PaperWorkspace demo paperId="attention" initialPage={2} />);
    const desktop = within(container.querySelector(".workspace-desktop") as HTMLElement);
    fireEvent.click(desktop.getByRole("button", { name: "翻译全文" }));
    fireEvent.click(screen.getByRole("button", { name: "确认并开始翻译" }));
    expect(await desktop.findByText("处理结束：成功 11 页，失败 2 页")).toBeVisible();
    expect(interval.mock.calls.some((call) => call[1] === 2_000)).toBe(false);
    expect(desktop.queryByRole("button", { name: "取消后台翻译" })).not.toBeInTheDocument();
    fireEvent.click(desktop.getByRole("button", { name: "重试失败页" }));
    expect(screen.getByRole("dialog", { name: "翻译整篇论文" })).toBeVisible();
  });

  it("首次创建失败在确认弹窗内显示，处理中不能关闭", async () => {
    let rejectCreate: ((reason: unknown) => void) | undefined;
    vi.spyOn(demoDataSource, "createPaperTranslation").mockImplementation(() => new Promise((_resolve, reject) => { rejectCreate = reject; }));
    const { container } = render(<PaperWorkspace demo paperId="attention" initialPage={2} />);
    const desktop = within(container.querySelector(".workspace-desktop") as HTMLElement);
    fireEvent.click(desktop.getByRole("button", { name: "翻译全文" }));
    fireEvent.click(screen.getByRole("button", { name: "确认并开始翻译" }));
    expect(screen.getByRole("button", { name: "关闭翻译确认" })).toBeDisabled();
    await act(async () => rejectCreate?.(new Error("翻译模型暂时不可用")));
    expect(screen.getByRole("dialog", { name: "翻译整篇论文" })).toHaveTextContent("翻译模型暂时不可用");
    expect(screen.getByRole("button", { name: "关闭翻译确认" })).toBeEnabled();
  });
});
