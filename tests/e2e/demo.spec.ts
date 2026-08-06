import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("公开演示可以提问并通过引用跳到论文页", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.getByRole("main", { name: "PaperLeaf 文献阅读工作台" })).toBeVisible();
  await expect(page.locator('.paper-workspace[data-client-ready="true"]')).toBeVisible();
  const mobile = page.viewportSize()!.width <= 900;
  if (mobile) {
    await page.locator(".mobile-workspace-tabs").getByRole("button", { name: /^提问/ }).click();
    await expect(page.locator('.mobile-workspace-tabs button[aria-current="page"]')).toContainText("提问");
  }
  const assistant = mobile ? page.locator(".workspace-mobile .workspace-assistant.mobile-active") : page.locator(".workspace-desktop .workspace-assistant");
  const citation = assistant.getByRole("button", { name: /PDF 6/ });
  await expect(citation).toBeVisible();
  await citation.click();
  if (mobile) await expect(page.locator('.mobile-workspace-tabs button[aria-current="page"]')).toContainText("论文");
  const reader = mobile ? page.locator(".workspace-mobile .workspace-reader.mobile-active") : page.locator(".workspace-desktop .workspace-reader");
  await expect(reader.getByText("6 / 15")).toBeVisible();
});

test("跨文献提问持久化运行状态并跳到引用物理页", async ({ page }) => {
  await page.goto("/ask");
  await expect(page.locator('.ask-layout[data-client-ready="true"]')).toBeVisible();
  await page.getByPlaceholder(/输入问题/).fill("作者为什么放弃循环结构？");
  await page.getByRole("button", { name: "发送问题" }).click();

  const trace = page.getByLabel("问答处理阶段");
  await expect(trace).toBeVisible();
  await expect(trace).toContainText("检索");
  await expect(page.getByText(/离开页面不会中断|回答已完成并持久化/)).toBeVisible();
  await page.getByRole("button", { name: /Attention Is All You Need.*PDF 2/ }).first().click();

  await expect(page).toHaveURL(/\/library\/attention\?page=2/);
  const mobile = page.viewportSize()!.width <= 900;
  const reader = mobile ? page.locator(".workspace-mobile .workspace-reader.mobile-active") : page.locator(".workspace-desktop .workspace-reader");
  await expect(reader.getByText("2 / 15")).toBeVisible();
});

test("首页没有严重无障碍问题", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /让每一次回答/ })).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(results.violations).toEqual([]);
});

test("论文工作台没有 serious 或 critical 无障碍问题", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.getByRole("main", { name: "PaperLeaf 文献阅读工作台" })).toBeVisible();
  await expect(page.locator('.paper-workspace[data-client-ready="true"]')).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(results.violations.filter((item) => item.impact === "serious" || item.impact === "critical")).toEqual([]);
});

test("论文工作台分隔条和引用保留完整可访问语义", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.locator('.paper-workspace[data-client-ready="true"]')).toBeVisible();
  const mobile = page.viewportSize()!.width <= 900;
  if (!mobile) {
    const separators = page.locator('.workspace-desktop [role="separator"]');
    await expect(separators).toHaveCount(2);
    for (const separator of await separators.all()) {
      await expect(separator).toHaveAttribute("aria-valuemin", /\d+/);
      await expect(separator).toHaveAttribute("aria-valuemax", /\d+/);
      await expect(separator).toHaveAttribute("aria-valuenow", /\d+/);
    }
  }
  if (mobile) await page.locator(".mobile-workspace-tabs").getByRole("button", { name: /^提问/ }).click();
  const assistant = mobile ? page.locator(".workspace-mobile .workspace-assistant.mobile-active") : page.locator(".workspace-desktop .workspace-assistant");
  await expect(assistant.getByRole("button", { name: /PDF 2/ }).first()).toBeVisible();
});

test("跨文献后台问答离开页面后继续，返回时恢复会话", async ({ page }) => {
  await page.goto("/ask");
  const input = page.getByPlaceholder(/输入问题/);
  await input.fill("离开页面后仍要完成的问题");
  await page.getByRole("button", { name: "发送问题" }).click();
  const submittedQuestion = page.locator(".chat-message.user > p").filter({ hasText: "离开页面后仍要完成的问题" });
  await expect(submittedQuestion).toHaveText("离开页面后仍要完成的问题");
  await page.goto("/library?demo=1");
  await page.waitForTimeout(1100);
  await page.goto("/ask");
  await expect(page.locator(".chat-message.user > p").filter({ hasText: "离开页面后仍要完成的问题" })).toHaveText("离开页面后仍要完成的问题");
  await expect(page.locator(".safe-markdown").filter({ hasText: "核验结论" })).toBeVisible();
});

test("公开演示可以生成证据化概览和结构图", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.locator('.paper-workspace[data-client-ready="true"]')).toBeVisible();
  const mobile = page.viewportSize()!.width <= 900;
  if (mobile) await page.locator(".mobile-workspace-tabs").getByRole("button", { name: /^提问/ }).click();
  const assistant = mobile ? page.locator(".workspace-mobile .workspace-assistant.mobile-active") : page.locator(".workspace-desktop .workspace-assistant");

  await assistant.getByRole("button", { name: "概览" }).click();
  await assistant.getByRole("button", { name: "生成概览" }).click();
  await expect(assistant.getByText("模型归纳")).toBeVisible();
  await assistant.getByRole("button", { name: /PDF 11/ }).click();
  if (mobile) await expect(page.locator('.mobile-workspace-tabs button[aria-current="page"]')).toContainText("论文");
  const reader = mobile ? page.locator(".workspace-mobile .workspace-reader.mobile-active") : page.locator(".workspace-desktop .workspace-reader");
  await expect(reader.getByText("11 / 15")).toBeVisible();

  if (mobile) await page.locator(".mobile-workspace-tabs").getByRole("button", { name: /^提问/ }).click();
  await assistant.getByRole("button", { name: "结构" }).click();
  await assistant.getByRole("button", { name: "构建结构" }).click();
  await expect(assistant.getByRole("button", { name: /循环结构限制并行与长程建模/ })).toBeVisible();
});

test("文献设置可以编辑元数据且删除需要二次确认", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.locator('.paper-workspace[data-client-ready="true"]')).toBeVisible();
  const mobile = page.viewportSize()!.width <= 900;
  if (mobile) await page.locator(".mobile-workspace-tabs").getByRole("button", { name: /^信息/ }).click();
  const info = mobile ? page.locator(".workspace-mobile .workspace-info.mobile-active") : page.locator(".workspace-desktop .workspace-info");
  await info.getByRole("button", { name: "编辑文献信息" }).click();
  const dialog = page.getByRole("dialog", { name: "文献设置" });
  await dialog.getByLabel("标题").fill("Attention, Revisited");
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(dialog.getByText("文献信息已保存。")).toBeVisible();
  await dialog.getByRole("button", { name: "删除文献" }).click();
  await expect(dialog.getByRole("button", { name: "确认删除" })).toBeVisible();
  await expect(dialog.getByText(/再次点击“确认删除”/)).toBeVisible();
  await dialog.getByRole("button", { name: "确认删除" }).click();
  await expect(dialog).toBeHidden();
  await expect(info.getByText("正在删除")).toBeVisible();
  await expect(info.getByText(/演示模式已模拟删除/)).toBeVisible();
  await info.getByRole("button", { name: "编辑文献信息" }).click();
  await expect(page.getByRole("dialog", { name: "文献设置" })).toBeVisible();
});

test("PDF 工具栏支持缩放、专注阅读和可恢复的逐页双栏翻译", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.locator('.paper-workspace[data-client-ready="true"]')).toBeVisible();
  const mobile = page.viewportSize()!.width <= 900;
  const desktop = page.locator(".workspace-desktop");
  const reader = mobile
    ? page.locator(".workspace-mobile .workspace-reader.mobile-active")
    : desktop.locator(".workspace-reader");
  await expect(reader.getByRole("toolbar", { name: "PDF 阅读工具栏" })).toBeVisible();

  const zoomOut = reader.getByRole("button", { name: "缩小 PDF" });
  for (let index = 0; index < 5; index += 1) await zoomOut.click();
  await expect(reader.getByText("50%", { exact: true })).toBeVisible();
  await expect(zoomOut).toBeDisabled();
  await reader.getByRole("button", { name: "适合宽度" }).click();
  await expect(reader.getByRole("button", { name: "适合宽度" })).toHaveAttribute("aria-pressed", "true");

  if (!mobile) {
    await reader.getByText("阅读布局", { exact: true }).click();
    await reader.getByRole("button", { name: "专注阅读" }).click();
    await expect(desktop.locator(".workspace-info")).toHaveCount(0);
    await expect(desktop.locator(".workspace-assistant")).toHaveCount(0);
    await expect(reader.getByText("2 / 15")).toBeVisible();
    await reader.getByRole("button", { name: "退出专注阅读" }).click();
    await expect(desktop.locator(".workspace-info")).toBeVisible();
    await expect(desktop.locator(".workspace-assistant")).toBeVisible();
  }

  await reader.getByRole("button", { name: "翻译全文" }).click();
  const dialog = page.getByRole("dialog", { name: "翻译整篇论文" });
  await expect(dialog).toContainText("15 页原文");
  for (const button of await dialog.getByRole("button").all()) {
    expect((await button.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  }
  await dialog.getByRole("button", { name: "确认并开始翻译" }).click();
  const translatedPage = reader.getByLabel("简体中文译文，第 2 页");
  await expect(translatedPage).toBeVisible();
  await expect(reader.getByText(/本文提出 Transformer/)).toBeVisible();
  const readerWidth = await reader.evaluate((element) => element.getBoundingClientRect().width);
  if (readerWidth <= 720) await expect(reader.locator(".translation-original")).toBeHidden();
  const geometry = await translatedPage.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const host = element.closest(".workspace-reader")!.getBoundingClientRect();
    return { left: box.left, right: box.right, hostLeft: host.left, hostRight: host.right };
  });
  expect(geometry.left).toBeGreaterThanOrEqual(geometry.hostLeft - 1);
  expect(geometry.right).toBeLessThanOrEqual(geometry.hostRight + 1);
  const translatedAxe = await new AxeBuilder({ page }).include(".workspace-reader").withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(translatedAxe.violations.filter((item) => item.impact === "serious" || item.impact === "critical")).toEqual([]);
  await reader.getByRole("button", { name: "下一页" }).click();
  await expect(reader.getByLabel("简体中文译文，第 3 页")).toBeVisible();
  await expect(reader.getByText("第 3 页译文已缓存。公式、引用编号与专有名词会尽量保持原样。")).toBeVisible();

  const globalOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(globalOverflow).toBeLessThanOrEqual(1);
  if (page.viewportSize()!.width <= 390) {
    const toolbar = reader.locator(".reader-toolbar");
    const box = await toolbar.evaluate((element) => ({ scroll: element.scrollWidth, client: element.clientWidth }));
    expect(box.scroll).toBeGreaterThan(box.client);
  }
});

test("持久问答在 390/768 与 200% 等效视口下完整可操作", async ({ page }) => {
  const initialViewport = page.viewportSize();
  if (!initialViewport) throw new Error("响应式门禁需要固定 Playwright 视口");
  test.skip(![390, 768].includes(initialViewport.width), "只在移动端与平板门禁视口运行");

  await page.goto("/ask");
  const workspace = page.locator('.chat-workspace[data-active-run="false"]');
  await expect(workspace).toBeVisible();

  const historyButton = workspace.getByRole("button", { name: "历史" });
  const newSessionButton = workspace.getByRole("button", { name: "新对话" });
  const sendButton = workspace.getByRole("button", { name: "发送问题" });
  for (const control of [historyButton, newSessionButton, sendButton]) {
    const box = await control.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
  }

  if (initialViewport.width === 390) {
    await expect(workspace.getByRole("complementary", { name: "历史对话" })).toHaveCount(0);
    await historyButton.click();
    await expect(workspace.getByRole("complementary", { name: "历史对话" })).toBeVisible();
    await workspace.getByRole("button", { name: "关闭历史对话" }).click();
  }

  await newSessionButton.click();
  const closeHistory = workspace.getByRole("button", { name: "关闭历史对话" });
  if (await closeHistory.isVisible().catch(() => false)) await closeHistory.click();
  const prompt = workspace.getByRole("button", { name: "比较这些论文所采用的方法与关键假设" });
  await expect(prompt).toBeVisible();
  expect((await prompt.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  await prompt.click();
  await expect(workspace.getByPlaceholder(/输入问题/)).toHaveValue("比较这些论文所采用的方法与关键假设");
  await expect(sendButton).toBeEnabled();
  await expect(workspace).toHaveAttribute("data-active-run", "false");

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);

  if (initialViewport.width === 768) {
    // 768 CSS px 缩为一半，等效验证浏览器 200% 放大时的 384 CSS px 重排。
    await page.setViewportSize({ width: 384, height: 512 });
    const narrowCloseHistory = workspace.getByRole("button", { name: "关闭历史对话" });
    if (await narrowCloseHistory.isVisible().catch(() => false)) await narrowCloseHistory.click();
    await expect(workspace.getByPlaceholder(/输入问题/)).toBeVisible();
    await expect(sendButton).toBeVisible();
    const zoomedOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(zoomedOverflow).toBeLessThanOrEqual(1);
  }
});
