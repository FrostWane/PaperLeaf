import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("文献组织数量来自真实状态且支持批量整理、归档与恢复", async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto("/library?demo=1");
  const compact = page.viewportSize()!.width < 901;
  await expect(page.getByRole("heading", { name: "你的研究文献" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /全部文献\s*4/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /最近阅读\s*3/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /待整理\s*1/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /已归档\s*1/ })).toBeVisible();
  if (!compact) await expect(page.getByRole("columnheader", { name: "出版物" })).toBeVisible();
  await expect(page.getByText("最近阅读 2026-07-28")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  expect(await page.locator(".table-scroll").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);

  await page.getByRole("tab", { name: /待整理/ }).click();
  await expect(page.locator(".paper-cell").filter({ hasText: "LoRA" })).toBeVisible();
  await expect(page.locator(".paper-cell").filter({ hasText: "Attention Is All You Need" })).toBeHidden();

  if (!compact) {
    const tree = page.getByRole("tree", { name: "集合树" });
    await expect(tree.getByRole("treeitem", { name: /全部文献\s*4/ })).toHaveAttribute("aria-level", "1");
    await expect(tree.getByRole("treeitem", { name: /核心方法\s*3/ })).toHaveAttribute("aria-level", "2");
    await tree.getByRole("treeitem", { name: /核心方法\s*3/ }).press("ArrowRight");
    await expect(tree.getByRole("treeitem", { name: /Transformer\s*1/ })).toBeVisible();
    await tree.getByRole("treeitem", { name: /全部文献\s*4/ }).press("ArrowLeft");
    await expect(tree.getByRole("treeitem", { name: /核心方法/ })).toBeHidden();
    await tree.getByRole("treeitem", { name: /全部文献/ }).press("ArrowRight");
  }

  if (compact) await page.getByRole("button", { name: "组织" }).click();
  else await page.getByLabel("管理集合").click();
  const dialog = page.getByRole("dialog", { name: "管理集合" });
  await dialog.getByLabel("名称").fill("批量精读");
  await dialog.getByLabel("说明（可选）").fill("本周需要完成的论文");
  await dialog.getByRole("button", { name: "创建", exact: true }).click();
  await expect(dialog.getByText("集合已创建。")).toBeVisible();
  await dialog.getByLabel("关闭").click();

  await page.getByRole("tab", { name: /全部文献/ }).click();
  const ragRow = page.getByRole("row").filter({ hasText: "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks" });
  await ragRow.getByRole("checkbox").check();
  await page.getByRole("button", { name: "重新识别与索引" }).click();
  const reindexDialog = page.getByRole("dialog", { name: /重新识别并索引 1 篇文献/ });
  await expect(reindexDialog.getByText(/复用已保存的原始 PDF/)).toBeVisible();
  await reindexDialog.getByRole("button", { name: "确认重新处理" }).click();
  await expect(page.getByText("已将 1 篇文献加入重新识别与索引队列。")).toBeVisible();

  await ragRow.getByRole("checkbox").check();
  await page.getByLabel("选择集合").selectOption({ label: "批量精读（0）" });
  await page.getByRole("button", { name: "加入集合" }).click();
  await expect(page.getByText("已整理 1 篇文献。")).toBeVisible();

  if (compact) await page.locator(".mobile-organization-filters").getByLabel("集合").selectOption({ label: "批量精读（1）" });
  else await page.getByRole("complementary", { name: "集合" }).getByRole("treeitem", { name: /批量精读.*1/ }).click();
  await expect(page.locator(".paper-cell").filter({ hasText: "Retrieval-Augmented Generation" })).toBeVisible();
  await expect(page.locator(".paper-cell").filter({ hasText: "Attention Is All You Need" })).toBeHidden();

  await page.getByRole("checkbox", { name: /选择 Retrieval-Augmented/ }).check();
  await page.getByRole("button", { name: "移出当前集合" }).click();
  await expect(page.getByText("没有匹配的论文")).toBeVisible();

  if (compact) await page.locator(".mobile-organization-filters").getByLabel("集合").selectOption("");
  else await page.getByRole("complementary", { name: "集合" }).getByRole("treeitem", { name: /全部文献/ }).click();
  const attentionRow = page.getByRole("row").filter({ hasText: "Attention Is All You Need" });
  await attentionRow.getByRole("checkbox").check();
  await page.getByRole("button", { name: "归档", exact: true }).click();
  await expect(page.getByText("已归档 1 篇文献。")).toBeVisible();
  await expect(page.getByRole("tab", { name: /已归档\s*2/ })).toBeVisible();

  await page.getByRole("tab", { name: /已归档/ }).click();
  await page.getByRole("checkbox", { name: /选择 Attention Is All You Need/ }).check();
  await page.getByRole("button", { name: "恢复", exact: true }).click();
  await expect(page.getByText("已恢复 1 篇文献。")).toBeVisible();
  await expect(page.getByRole("tab", { name: /已归档\s*1/ })).toBeVisible();
});

test("文献组织界面没有 serious 或 critical 无障碍问题", async ({ page }) => {
  await page.goto("/library?demo=1");
  await expect(page.getByRole("heading", { name: "你的研究文献" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /全部文献\s*4/ })).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(results.violations.filter((item) => item.impact === "serious" || item.impact === "critical")).toEqual([]);
});

test("已有论文可以直接查看、添加和移除集合", async ({ page }) => {
  await page.goto("/library?demo=1");
  await expect(page.getByRole("heading", { name: "你的研究文献" })).toBeVisible();

  const row = page.getByRole("row").filter({ hasText: "Attention Is All You Need" });
  await row.getByRole("button", { name: "管理 Attention Is All You Need 的集合" }).click();
  const dialog = page.getByRole("dialog", { name: "管理论文集合" });
  await expect(dialog.getByRole("checkbox", { name: /核心方法/ })).toBeChecked();
  await expect(dialog.getByRole("checkbox", { name: /实验参考/ })).not.toBeChecked();

  await dialog.getByRole("checkbox", { name: /核心方法/ }).uncheck();
  await dialog.getByRole("checkbox", { name: /实验参考/ }).check();
  await dialog.getByRole("button", { name: "保存集合" }).click();

  await expect(page.getByText("已更新《Attention Is All You Need》的集合。")).toBeVisible();
  if (page.viewportSize()!.width >= 901) {
    await expect(row.getByText("实验参考")).toBeVisible();
    await expect(row.getByText("核心方法")).toBeHidden();
  } else {
    await row.getByRole("button", { name: "管理 Attention Is All You Need 的集合" }).click();
    await expect(dialog.getByRole("checkbox", { name: /实验参考/ })).toBeChecked();
    await expect(dialog.getByRole("checkbox", { name: /核心方法/ })).not.toBeChecked();
  }
});
