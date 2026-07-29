import type { Meta, StoryObj } from "@storybook/react-vite";
import { LibraryOrganizerDialog } from "./library-organizer-dialog";

const meta = {
  title: "PaperLeaf/组织管理弹层",
  component: LibraryOrganizerDialog,
  args: {
    open: true,
    onOpenChange: () => undefined,
    collections: [
      { id: "core", name: "核心方法", description: "基础方法论文", paperIds: ["p1", "p2", "p3"] },
      { id: "reproduce", name: "实验复现", paperIds: ["p4"] },
    ],
    tags: [
      { id: "rag", name: "RAG", color: "#B8C9BC", paperIds: ["p1", "p4"] },
      { id: "nlp", name: "NLP", color: "#AFC3CE", paperIds: ["p2"] },
    ],
    onCreateCollection: async () => undefined,
    onUpdateCollection: async () => undefined,
    onDeleteCollection: async () => undefined,
    onCreateTag: async () => undefined,
    onUpdateTag: async () => undefined,
    onDeleteTag: async () => undefined,
  },
} satisfies Meta<typeof LibraryOrganizerDialog>;

export default meta;
type Story = StoryObj<typeof meta>;

export const 默认状态: Story = {};
