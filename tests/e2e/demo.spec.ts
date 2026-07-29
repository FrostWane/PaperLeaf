import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("公开演示可以提问并通过引用跳到论文页", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.getByRole("main", { name: "PaperLeaf 文献阅读工作台" })).toBeVisible();
  await expect(page.locator('.paper-workspace[data-client-ready="true"]')).toBeVisible();
  const mobile = page.viewportSize()!.width < 760;
  if (mobile) {
    await page.locator(".mobile-workspace-tabs").getByRole("button", { name: /^提问/ }).click();
    await expect(page.locator('.mobile-workspace-tabs button[aria-current="page"]')).toContainText("提问");
  }
  const assistant = mobile ? page.locator(".workspace-mobile .workspace-assistant.mobile-active") : page.locator(".workspace-desktop .workspace-assistant");
  const citation = assistant.getByRole("button", { name: "查看第 6 页引用" });
  await expect(citation).toBeVisible();
  await citation.click();
  if (mobile) await expect(page.locator('.mobile-workspace-tabs button[aria-current="page"]')).toContainText("论文");
  const reader = mobile ? page.locator(".workspace-mobile .workspace-reader.mobile-active") : page.locator(".workspace-desktop .workspace-reader");
  await expect(reader.getByText("06 / 15")).toBeVisible();
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

test("公开演示可以生成证据化概览和结构图", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.locator('.paper-workspace[data-client-ready="true"]')).toBeVisible();
  const mobile = page.viewportSize()!.width < 760;
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
  const mobile = page.viewportSize()!.width < 760;
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
