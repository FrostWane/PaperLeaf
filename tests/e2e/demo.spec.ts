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
