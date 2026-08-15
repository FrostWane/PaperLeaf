import fs from "node:fs";
import path from "node:path";
import process from "node:process";

import { chromium } from "@playwright/test";

const root = path.resolve(import.meta.dirname, "..");
const envPath = path.join(root, ".env");

function readLocalEnv() {
  if (!fs.existsSync(envPath)) return {};
  return Object.fromEntries(
    fs
      .readFileSync(envPath, "utf8")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#") && line.includes("="))
      .map((line) => {
        const separator = line.indexOf("=");
        return [line.slice(0, separator), line.slice(separator + 1).replace(/^['"]|['"]$/g, "")];
      }),
  );
}

const localEnv = readLocalEnv();
const baseURL = process.env.PAPERLEAF_SCREENSHOT_BASE_URL ?? "http://localhost:3000";
const email = process.env.PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL ?? localEnv.PAPERLEAF_BOOTSTRAP_ADMIN_EMAIL;
const password = process.env.PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD ?? localEnv.PAPERLEAF_BOOTSTRAP_ADMIN_PASSWORD;
const preferredPaperId = process.env.PAPERLEAF_SCREENSHOT_PAPER_ID ?? "89d95a3d-7a9b-461d-8b6f-23aeb32f4781";
const outputDir = path.join(root, "docs", "images");

if (!email || !password) {
  throw new Error("缺少本地管理员凭证，无法生成真实模式截图");
}

fs.mkdirSync(outputDir, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });

try {
  await page.goto(`${baseURL}/login`, { waitUntil: "networkidle" });
  await page.getByLabel("邮箱").fill(email);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "进入工作区" }).click();
  await page.waitForURL(/\/library/);

  await page.goto(`${baseURL}/library`, { waitUntil: "networkidle" });
  await page.locator("main").waitFor({ state: "visible" });
  await page.screenshot({ path: path.join(outputDir, "paperleaf-library.png") });
  await page.setViewportSize({ width: 1280, height: 640 });
  await page.screenshot({ path: path.join(outputDir, "paperleaf-social-preview.png") });
  await page.setViewportSize({ width: 1440, height: 900 });

  const discoveredPaperHref = await page.locator('a[href^="/library/"]').evaluateAll((links) =>
    links
      .map((link) => link.getAttribute("href"))
      .find((href) => href && /^\/library\/[^/?#]+$/.test(href)),
  );
  const preferredHref = `/library/${preferredPaperId}`;
  const paperHref = await page.locator(`a[href="${preferredHref}"]`).count()
    ? preferredHref
    : discoveredPaperHref;
  if (!paperHref) throw new Error("文献库中没有可打开的论文");
  await page.goto(`${baseURL}${paperHref}`, { waitUntil: "networkidle" });
  await page.locator(".paper-workspace").waitFor({ state: "visible", timeout: 60_000 });
  await page.screenshot({ path: path.join(outputDir, "paperleaf-cited-answer.png") });

  await page.goto(`${baseURL}/ask`, { waitUntil: "networkidle" });
  await page.locator(".chat-workspace").first().waitFor({ state: "visible", timeout: 60_000 });
  const targetSessionId = "ce4b7859-1db4-4b6f-91d8-ab58da8f37f8";
  const apiBase = await page.evaluate(async () => {
    const resource = performance.getEntriesByType("resource")
      .map((entry) => entry.name)
      .find((name) => name.includes("/api/v1/"));
    if (resource) return resource.slice(0, resource.indexOf("/api/v1/") + "/api/v1".length);
    for (const candidate of ["http://localhost:8000/api/v1", "http://127.0.0.1:8000/api/v1"]) {
      const response = await fetch(`${candidate}/auth/me`, { credentials: "include" });
      if (response.ok) return candidate;
    }
    return null;
  });
  if (!apiBase) throw new Error("无法从当前构建识别 API 地址");
  const target = await page.evaluate(async ({ sessionId, apiBase }) => {
    const [sessionsResponse, collectionsResponse] = await Promise.all([
      fetch(`${apiBase}/chat/sessions`, { credentials: "include" }),
      fetch(`${apiBase}/collections`, { credentials: "include" }),
    ]);
    if (!sessionsResponse.ok || !collectionsResponse.ok) {
      return { error: `API ${sessionsResponse.status}/${collectionsResponse.status}` };
    }
    const sessionsPayload = await sessionsResponse.json();
    const collectionsPayload = await collectionsResponse.json();
    const sessions = Array.isArray(sessionsPayload) ? sessionsPayload : sessionsPayload.items ?? [];
    const collections = Array.isArray(collectionsPayload) ? collectionsPayload : collectionsPayload.items ?? [];
    const session = sessions.find((item) => item.id === sessionId);
    const collection = collections.find((item) => item.id === session?.collection_id);
    return session ? { title: session.title, collectionName: collection?.name ?? null } : null;
  }, { sessionId: targetSessionId, apiBase });
  if (target?.error) throw new Error(`截图数据读取失败：${target.error}`);
  if (target) {
    if (target.collectionName) {
      await page.getByRole("treeitem", { name: new RegExp(`^${target.collectionName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s+\\d+$`) }).click();
    }
    for (let pageNumber = 1; pageNumber <= 40; pageNumber += 1) {
      const sessionButton = page.locator(".chat-session-select", { hasText: target.title });
      if (await sessionButton.count()) {
        await sessionButton.first().click();
        break;
      }
      const nextPage = page.getByRole("button", { name: "下一页" });
      if (!await nextPage.count() || await nextPage.isDisabled()) break;
      await nextPage.click();
    }
    const comparisonStatus = page.locator(".chat-parallel-status");
    await comparisonStatus.waitFor({ state: "visible", timeout: 60_000 });
    await comparisonStatus.scrollIntoViewIfNeeded();
  }
  await page.screenshot({ path: path.join(outputDir, "paperleaf-agent-trace.png") });
} finally {
  await browser.close();
}

console.log("release screenshots captured");
