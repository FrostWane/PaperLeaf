import type { Meta, StoryObj } from "@storybook/react-vite";
import { PaperWorkspace } from "./paper-workspace";

const meta = { title: "PaperLeaf/论文工作台", component: PaperWorkspace, parameters: { viewport: { defaultViewport: "responsive" } } } satisfies Meta<typeof PaperWorkspace>;
export default meta;
type Story = StoryObj<typeof meta>;
export const 默认回答: Story = { args: { paperId: "attention", demo: true } };
export const 无文本页翻译入口: Story = { args: { paperId: "attention", demo: true, initialPage: 7 } };
