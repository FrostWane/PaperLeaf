import { defineConfig } from "vitest/config";
import path from "node:path";

export default defineConfig({
  resolve: { alias: { "@": path.resolve(__dirname, ".") } },
  test: { environment: "jsdom", setupFiles: ["./tests/setup.ts"], include: ["tests/**/*.test.{ts,tsx}"], restoreMocks: true, pool: "threads", maxWorkers: 1, fileParallelism: false },
});
