import type { Meta, StoryObj } from "@storybook/react-vite";
import { UploadDialog } from "./upload-dialog";

const meta = { title: "PaperLeaf/上传文献", component: UploadDialog, decorators: [(Story) => <div style={{ padding: 40 }}><Story /></div>] } satisfies Meta<typeof UploadDialog>;
export default meta;
type Story = StoryObj<typeof meta>;
export const 默认: Story = {};
