import { expect, test } from "@playwright/test";

test("2K 工作台使用可读的宽屏字号", async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 0) < 2000, "仅在 2K 项目中验证宽屏排版");

  await page.goto("/demo");
  await expect(page.locator('.paper-workspace[data-client-ready="true"]')).toBeVisible();

  const sizeOf = async (selector: string) =>
    page.locator(`.workspace-desktop ${selector}`).first().evaluate((element) =>
      Number.parseFloat(getComputedStyle(element).fontSize),
    );

  expect(await sizeOf(".answer-text")).toBeGreaterThanOrEqual(14);
  expect(await sizeOf(".citation-row q")).toBeGreaterThanOrEqual(11);
  expect(await sizeOf(".composer-box textarea")).toBeGreaterThanOrEqual(13);
  expect(await sizeOf(".paper-summary p")).toBeGreaterThanOrEqual(11);
  expect(await sizeOf(".reader-status")).toBeGreaterThanOrEqual(10);
  expect(await sizeOf(".mock-paper > p:not(.pdf-authors)")).toBeGreaterThanOrEqual(12);

  const layout = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
  }));
  expect(layout.document).toBeLessThanOrEqual(layout.viewport);
});
