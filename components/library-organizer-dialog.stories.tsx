import type { Meta, StoryObj } from "@storybook/react-vite";
import { LibraryOrganizerDialog } from "./library-organizer-dialog";

const meta = {
  title: "PaperLeaf/组织管理弹层",
  component: LibraryOrganizerDialog,
  args: {
    open: true,
    onOpenChange: () => undefined,
    collections: [
      { id: "core", name: "核心方法", description: "基础方法论文", parentId: null, paperIds: ["p1"], recursivePaperCount: 3, children: [
        { id: "rag", name: "检索增强", parentId: "core", paperIds: ["p2", "p3"], recursivePaperCount: 2, children: [] },
      ] },
      { id: "reproduce", name: "实验复现", parentId: null, paperIds: ["p4"], recursivePaperCount: 1, children: [] },
    ],
    onCreateCollection: async () => undefined,
    onUpdateCollection: async () => undefined,
    onDeleteCollection: async () => undefined,
  },
} satisfies Meta<typeof LibraryOrganizerDialog>;

export default meta;
type Story = StoryObj<typeof meta>;

export const 默认状态: Story = {};
