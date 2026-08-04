import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("文献组织数量来自真实状态且支持批量整理、归档与恢复", async ({ page }) => {
  test.setTimeout(60_000);
  await page.goto("/library?demo=1");
  await expect(page.getByRole("heading", { name: "你的研究文献" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /全部文献\s*4/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /最近阅读\s*3/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /待整理\s*1/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /已归档\s*1/ })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  expect(await page.locator(".table-scroll").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  const compact = page.viewportSize()!.width < 901;

  await page.getByRole("tab", { name: /待整理/ }).click();
  await expect(page.locator(".paper-cell").filter({ hasText: "LoRA" })).toBeVisible();
  await expect(page.locator(".paper-cell").filter({ hasText: "Attention Is All You Need" })).toBeHidden();

  if (compact) await page.getByRole("button", { name: "组织" }).click();
  else await page.getByLabel("管理集合和标签").click();
  const dialog = page.getByRole("dialog", { name: "管理文献组织" });
  await dialog.getByLabel("名称").fill("批量精读");
  await dialog.getByLabel("说明（可选）").fill("本周需要完成的论文");
  await dialog.getByRole("button", { name: "创建", exact: true }).click();
  await expect(dialog.getByText("集合已创建。")).toBeVisible();

  await dialog.getByRole("tab", { name: "标签" }).click();
  await dialog.getByLabel("名称").fill("方法综述");
  await dialog.getByRole("button", { name: "创建", exact: true }).click();
  await expect(dialog.getByText("标签已创建。")).toBeVisible();
  await dialog.getByLabel("编辑 方法综述").click();
  await dialog.getByLabel("名称").fill("综述必读");
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(dialog.getByText("综述必读")).toBeVisible();
  await dialog.getByLabel("删除 综述必读").click();
  await dialog.getByLabel("确认删除 综述必读").click();
  await expect(dialog.getByText("标签已删除，文献本身仍保留。")).toBeVisible();
  await dialog.getByLabel("关闭").click();

  await page.getByRole("tab", { name: /全部文献/ }).click();
  const ragRow = page.getByRole("row").filter({ hasText: "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks" });
  await ragRow.getByRole("checkbox").check();
  await page.getByLabel("选择集合").selectOption({ label: "批量精读" });
  await page.getByRole("button", { name: "加入集合" }).click();
  await expect(page.getByText("已整理 1 篇文献。")).toBeVisible();

  if (compact) await page.locator(".mobile-organization-filters").getByLabel("集合").selectOption({ label: "批量精读（1）" });
  else await page.getByRole("complementary", { name: "集合和标签" }).getByRole("button", { name: /批量精读/ }).click();
  await expect(page.locator(".paper-cell").filter({ hasText: "Retrieval-Augmented Generation" })).toBeVisible();
  await expect(page.locator(".paper-cell").filter({ hasText: "Attention Is All You Need" })).toBeHidden();

  await page.getByRole("checkbox", { name: /选择 Retrieval-Augmented/ }).check();
  await page.getByRole("button", { name: "移出当前集合" }).click();
  await expect(page.getByText("没有匹配的论文")).toBeVisible();

  if (compact) await page.locator(".mobile-organization-filters").getByLabel("集合").selectOption("all");
  else await page.getByRole("complementary", { name: "集合和标签" }).getByRole("button", { name: "全部集合" }).click();
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
