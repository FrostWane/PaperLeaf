import { expect, test } from "@playwright/test";

test("2K 工作台使用可读的宽屏字号", async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 0) < 2000, "仅在 2K 项目中验证宽屏排版");

  await page.goto("/demo");
  await expect(page.locator('.paper-workspace[data-client-ready="true"]')).toBeVisible();

  const [sansFont, monoFont] = await Promise.all([
    page.request.get("/fonts/geist-sans/Geist-Variable.woff2"),
    page.request.get("/fonts/geist-mono/GeistMono-Variable.woff2"),
  ]);
  expect(sansFont.status()).toBe(200);
  expect(monoFont.status()).toBe(200);
  expect(sansFont.headers()["content-type"]).toContain("font/woff2");
  expect(monoFont.headers()["content-type"]).toContain("font/woff2");

  const sizeOf = async (selector: string) =>
    page.locator(`.workspace-desktop ${selector}`).first().evaluate((element) =>
      Number.parseFloat(getComputedStyle(element).fontSize),
    );

  expect(await sizeOf(".safe-markdown")).toBeGreaterThanOrEqual(14);
  expect(await sizeOf(".chat-message > span")).toBeGreaterThanOrEqual(12);
  expect(await sizeOf(".chat-citations small")).toBeGreaterThanOrEqual(12);
  expect(await sizeOf(".chat-composer textarea")).toBeGreaterThanOrEqual(14);
  expect(await sizeOf(".paper-summary p")).toBeGreaterThanOrEqual(12);
  expect(await sizeOf(".reader-status")).toBeGreaterThanOrEqual(12);
  expect(await sizeOf(".mock-paper > p:not(.pdf-authors)")).toBeGreaterThanOrEqual(14);

  const layout = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
  }));
  expect(layout.document).toBeLessThanOrEqual(layout.viewport);

  await page.goto("/settings");
  for (const option of [
    { name: /小\s*适合高密度/, mode: "small", body: 14, support: 12 },
    { name: /标准\s*默认阅读/, mode: "standard", body: 15, support: 13 },
    { name: /大\s*适合 2K/, mode: "large", body: 17, support: 15 },
  ]) {
    await page.getByRole("radio", { name: option.name }).click();
    const scale = await page.evaluate(() => ({
      mode: document.documentElement.dataset.fontScale,
      body: Number.parseFloat(getComputedStyle(document.querySelector(".setting-title p")!).fontSize),
      support: Number.parseFloat(getComputedStyle(document.querySelector(".settings-form-row small")!).fontSize),
    }));
    expect(scale.mode).toBe(option.mode);
    expect(scale.body).toBeGreaterThanOrEqual(option.body);
    expect(scale.support).toBeGreaterThanOrEqual(option.support);
  }

  await page.goto("/admin");
  for (const selector of [".metric-row span", ".metric-row small", ".data-table th", ".runtime-purposes small", ".job-row small"]) {
    const size = await page.locator(selector).first().evaluate((element) => Number.parseFloat(getComputedStyle(element).fontSize));
    expect(size, selector).toBeGreaterThanOrEqual(12);
  }
  await expect(page.locator(".metric-row strong").first()).toHaveCSS("font-size", "24px");
  const adminHierarchy = await page.evaluate(() => {
    const sectionTitle = getComputedStyle(document.querySelector(".section-bar h2")!);
    const itemTitle = getComputedStyle(document.querySelector(".admin-user strong")!);
    const jobStatus = document.querySelector(".job-row > .mono") as HTMLElement;
    const statusStyle = getComputedStyle(jobStatus);
    return {
      sectionSize: Number.parseFloat(sectionTitle.fontSize),
      sectionWeight: Number.parseInt(sectionTitle.fontWeight, 10),
      itemSize: Number.parseFloat(itemTitle.fontSize),
      itemWeight: Number.parseInt(itemTitle.fontWeight, 10),
      jobHeight: jobStatus.getBoundingClientRect().height,
      jobLineHeight: Number.parseFloat(statusStyle.lineHeight),
      jobWhiteSpace: statusStyle.whiteSpace,
    };
  });
  expect(adminHierarchy.sectionSize).toBeGreaterThan(adminHierarchy.itemSize);
  expect(adminHierarchy.sectionWeight).toBeGreaterThanOrEqual(adminHierarchy.itemWeight);
  expect(adminHierarchy.jobWhiteSpace).toBe("nowrap");
  expect(adminHierarchy.jobHeight).toBeLessThanOrEqual(adminHierarchy.jobLineHeight * 1.5);

  await page.goto("/library?demo=1");
  for (const selector of [".collection-tabs span", ".collection-tree-item small", ".data-table th"]) {
    const size = await page.locator(selector).first().evaluate((element) => Number.parseFloat(getComputedStyle(element).fontSize));
    expect(size, selector).toBeGreaterThanOrEqual(12);
  }
  const libraryGeometry = await page.evaluate(() => {
    const year = document.querySelector(".library-data-table th:nth-child(5) .sortable") as HTMLElement;
    const header = document.querySelector(".library-data-table th") as HTMLElement;
    const title = document.querySelector(".library-data-table .paper-cell strong") as HTMLElement;
    return {
      yearWidth: year.clientWidth,
      yearScrollWidth: year.scrollWidth,
      yearWhiteSpace: getComputedStyle(year).whiteSpace,
      headerFont: getComputedStyle(header).fontFamily,
      titleMaxWidth: getComputedStyle(title).maxWidth,
    };
  });
  expect(libraryGeometry.yearWhiteSpace).toBe("nowrap");
  expect(libraryGeometry.yearScrollWidth).toBeLessThanOrEqual(libraryGeometry.yearWidth);
  expect(libraryGeometry.headerFont).toContain("PaperLeaf CJK");
  expect(libraryGeometry.titleMaxWidth).toBe("100%");
  const finalLayout = await page.evaluate(() => ({ viewport: window.innerWidth, document: document.documentElement.scrollWidth }));
  expect(finalLayout.document).toBeLessThanOrEqual(finalLayout.viewport);
});

test("移动端账户、退出与偏好控件保持可达触摸目标", async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 0) >= 760, "仅在移动端项目中验证账户入口");

  await page.goto("/library?demo=1");
  const accountLink = page.getByRole("link", { name: /账户与退出设置/ });
  await expect(accountLink).toBeVisible();
  expect((await accountLink.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  await accountLink.click();
  await expect(page).toHaveURL(/\/settings$/);

  const controls = [
    page.getByRole("button", { name: "保存个人设置" }),
    page.getByRole("switch", { name: "默认展开文献资料" }),
    page.getByRole("button", { name: "退出登录" }),
  ];
  for (const control of controls) {
    await control.scrollIntoViewIfNeeded();
    await expect(control).toBeVisible();
    expect((await control.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  }

  const layout = await page.evaluate(() => ({ viewport: window.innerWidth, document: document.documentElement.scrollWidth }));
  expect(layout.document).toBeLessThanOrEqual(layout.viewport);
});
