import type { Meta, StoryObj } from "@storybook/react-vite";
import { LibraryTable } from "./library-table";

const meta = {
  title: "PaperLeaf/文献组织工作台",
  component: LibraryTable,
  decorators: [(Story) => <main style={{ padding: 32, minHeight: "100vh" }}><Story /></main>],
  parameters: { viewport: { defaultViewport: "responsive" } },
} satisfies Meta<typeof LibraryTable>;

export default meta;
type Story = StoryObj<typeof meta>;

export const 固定数据: Story = { args: { demo: true } };
