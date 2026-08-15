import { expect, test } from "@playwright/test";

async function login(page: import("@playwright/test").Page, email: string, password: string) {
  await page.goto("/login");
  await page.getByLabel("邮箱").fill(email);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "进入工作区" }).click();
  await page.waitForURL(/\/library/);
}

test("普通用户登录、阅读引用跳页并退出", async ({ page }) => {
  const email = process.env.PAPERLEAF_SMOKE_USER_EMAIL;
  const password = process.env.PAPERLEAF_SMOKE_USER_PASSWORD;
  const paperId = process.env.PAPERLEAF_SMOKE_PAPER_ID;
  const physicalPage = process.env.PAPERLEAF_SMOKE_CITATION_PAGE;
  if (!email || !password || !paperId || !physicalPage) throw new Error("full-stack smoke 环境不完整");

  await login(page, email, password);
  await expect(page.getByRole("link", { name: /管理/ })).toHaveCount(0);
  await page.goto(`/library/${encodeURIComponent(paperId)}?page=${physicalPage}`);
  await expect(page.getByLabel(new RegExp(`第 ${physicalPage} 页，共`)).first()).toBeVisible();
  await expect(page.getByText(`第 ${physicalPage} 页`, { exact: true }).first()).toBeVisible();
  await page.getByLabel(/打开 .* 的账户菜单/).click();
  await page.getByRole("button", { name: "退出登录" }).click();
  await page.waitForURL(/\/login/);
});

test("管理员能查看任务与刚完成的 RAG 质量记录", async ({ page }) => {
  const email = process.env.PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL;
  const password = process.env.PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD;
  const userEmail = process.env.PAPERLEAF_SMOKE_USER_EMAIL;
  if (!email || !password || !userEmail) throw new Error("管理员 full-stack smoke 环境不完整");

  await login(page, email, password);
  await page.goto("/admin");
  await page.getByRole("tab", { name: "用户与权限" }).click();
  await expect(page.getByText(userEmail)).toBeVisible();
  await page.getByRole("tab", { name: "后台任务" }).click();
  await expect(page.getByRole("heading", { name: "后台任务" })).toBeVisible();
  await page.getByRole("tab", { name: "RAG 质量" }).click();
  await expect(page.getByRole("button", { name: "7 天" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("heading", { name: "RAG 运行质量" })).toBeVisible();
  await expect(page.getByText("已采集运行", { exact: true })).toBeVisible();
  await expect(page.getByText(/还没有可分析|尚无可分析/)).toHaveCount(0);
});
