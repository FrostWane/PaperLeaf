import type { Metadata } from "next";
import { AppShell } from "@/components/app-shell";
import { PaperWorkspace } from "@/components/paper-workspace";

export const metadata: Metadata = { title: "论文工作台" };
export default async function PaperPage({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ demo?: string }> }) {
  const { id } = await params;
  const demo = (await searchParams).demo === "1";
  return <AppShell active="/library" title="论文工作台" eyebrow="Library / Reading" flush demo={demo}><PaperWorkspace key={id} paperId={id} demo={demo} /></AppShell>;
}
