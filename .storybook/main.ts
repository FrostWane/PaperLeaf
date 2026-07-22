import type { StorybookConfig } from "@storybook/react-vite";

const config: StorybookConfig = {
  stories: ["../components/**/*.stories.@(ts|tsx)"],
  addons: ["@storybook/addon-a11y"],
  framework: { name: "@storybook/react-vite", options: {} },
  core: {
    builder: {
      name: "@storybook/builder-vite",
      options: { viteConfigPath: ".storybook/vite.config.ts" },
    },
  },
};
export default config;
