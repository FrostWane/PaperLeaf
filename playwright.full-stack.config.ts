import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/full-stack",
  timeout: 90_000,
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: process.env.PAPERLEAF_SMOKE_WEB_URL,
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
