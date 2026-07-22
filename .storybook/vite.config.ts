import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { defineConfig } from "vite";

// Storybook 必须与 Vinext 的 RSC / Cloudflare 构建链隔离，否则预览构建会
// 误加载服务端资产清单插件。这里只保留组件实验室真正需要的能力。
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": resolve(process.cwd()) },
  },
});
