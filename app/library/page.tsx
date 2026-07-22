import type { Metadata } from "next";
import { AppShell } from "@/components/app-shell";
import { LibraryTable } from "@/components/library-table";
import { UploadDialog } from "@/components/upload-dialog";

export const metadata: Metadata = { title: "文献库" };
export default function LibraryPage() { return <AppShell active="/library" title="文献库" eyebrow="Library / 5 papers" actions={<UploadDialog />}><div className="page-lead"><div><h2>你的研究文献</h2><p>上传、筛选和打开论文；处理状态会在这里持续更新。</p></div><div className="collection-tabs"><button className="active">全部文献 <span>5</span></button><button>最近阅读 <span>3</span></button><button>待整理 <span>1</span></button></div></div><LibraryTable /></AppShell>; }
