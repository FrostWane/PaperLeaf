import type { Metadata } from "next";
import { AppShell } from "@/components/app-shell";
import { PaperWorkspace } from "@/components/paper-workspace";

export const metadata: Metadata = { title: "论文工作台" };
export default async function PaperPage({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ demo?: string; page?: string }> }) {
  const { id } = await params;
  const query = await searchParams;
  const demo = query.demo === "1";
  const requestedPage = Number.parseInt(query.page ?? "", 10);
  const initialPage = Number.isFinite(requestedPage) && requestedPage > 0 ? requestedPage : undefined;
  return <AppShell active="/library" title="论文工作台" flush demo={demo}><PaperWorkspace key={`${id}:${initialPage ?? "default"}`} paperId={id} demo={demo} initialPage={initialPage} /></AppShell>;
}
