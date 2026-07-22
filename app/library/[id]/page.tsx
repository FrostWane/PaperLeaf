import type { Metadata } from "next";
import { AppShell } from "@/components/app-shell";
import { PaperWorkspace } from "@/components/paper-workspace";

export const metadata: Metadata = { title: "论文工作台" };
export default async function PaperPage({ params }: { params: Promise<{ id: string }> }) { const { id } = await params; return <AppShell active="/library" title="论文工作台" eyebrow="Library / Reading" flush><PaperWorkspace paperId={id} /></AppShell>; }
