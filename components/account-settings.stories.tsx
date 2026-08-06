import type { Meta, StoryObj } from "@storybook/react-vite";
import { AdminView } from "./admin-view";
import { AppShell } from "./app-shell";
import { SettingsView } from "./settings-view";

function AccountSettingsStory() {
  return (
    <AppShell active="/settings" title="设置" eyebrow="个人偏好">
      <SettingsView />
    </AppShell>
  );
}

const meta = {
  title: "PaperLeaf/账户与个人设置",
  component: AccountSettingsStory,
  parameters: { viewport: { defaultViewport: "responsive" } },
} satisfies Meta<typeof AccountSettingsStory>;

export default meta;
type Story = StoryObj<typeof meta>;

export const 默认设置: Story = {};

export const 管理后台: Story = {
  render: () => (
    <AppShell active="/admin" title="管理" eyebrow="系统与权限">
      <AdminView />
    </AppShell>
  ),
};
