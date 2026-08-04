import { defineConfig, devices } from "@playwright/test";

const testPort = 3100;
const testBaseUrl = `http://localhost:${testPort}`;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  workers: process.env.CI ? 2 : 3,
  retries: process.env.CI ? 2 : 0,
  reporter: "list",
  use: { baseURL: testBaseUrl, trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "chromium-2k", use: { ...devices["Desktop Chrome"], viewport: { width: 2560, height: 1440 } } },
    { name: "chromium-desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "chromium-tablet", use: { ...devices["Desktop Chrome"], viewport: { width: 768, height: 1024 } } },
    { name: "chromium-mobile", use: { ...devices["Pixel 7"], viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    command: "pnpm test:e2e:server",
    url: testBaseUrl,
    reuseExistingServer: false,
    timeout: 300_000,
    env: { NEXT_PUBLIC_DATA_MODE: "demo" },
  },
});
